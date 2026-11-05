"""Command-line interface for paperpush.

Implemented so far:

    paperpush venues            list supported venues
    paperpush venues --validate list only the venues paperpush can validate against
    paperpush venues --submit   list only the venues paperpush can submit to
    paperpush venues -k KW -t TYPE
                                      filter by subject keywords and venue type
    paperpush pick-venue <keywords|manuscript>
                                      rank venues for a manuscript (--title, --abstract,
                                      --manuscript; --venue-name to search by name)
    paperpush subfile <venue>   create a <venue>.sub.txt template
    paperpush options <venue>.<field>
                                      list the allowed values for a field
    paperpush autofill -p <dir|file> [<dir|file> ...] <subfile>
                                      fill a .sub from a manuscript directory
    paperpush validate <subfile>  run the pre-submission checks on a .sub
    paperpush requirements <venue>  show what the venue's author guidelines require of the manuscript
                                      (scans referenced files for secrets, GPS
                                      metadata, LaTeX comments, broken links, and
                                      references whose DOI points at a different
                                      work, by default; --dont-check-* to opt
                                      out)
    paperpush login <venue>     store credentials for a venue
    paperpush login --list        list the venues you are logged in to
    paperpush login --orcid <j>   store an ORCID iD/password for a journal that
                                      offers "Sign in with ORCID"
    paperpush submit <subfile>    open bioRxiv and run the submission
    paperpush add-venue --template [<slug>]  write a venue file to fill in
    paperpush add-venue <file>    add a venue from a filled venue file
    paperpush update-venues       fetch the latest published venue data now
    paperpush install-skills      install the agent skills (Claude Code, Codex)
    paperpush --agent-guide       print the guide for AI agents (AGENTS.md)

``submit`` drives the bioRxiv wizard, typing in the field values read from the
.sub file and stopping before the final submit. The first run signs in (reusing
stored credentials where possible) and saves the browser session, so later runs
start already authenticated.
"""

from __future__ import annotations

import argparse
import contextlib
import getpass
import json
import logging
import os
import sys
import textwrap
from pathlib import Path

from pydantic import ConfigDict, validate_call

from . import __url__, __version__, credentials, venue_data
from ._logging import configure_logging
from .database import get_venue, list_venues
from .subfile import default_filename, is_subfile_name, subfile_stem, with_subfile_suffix, write_template
from .venues.common import DEFAULT_TIMEOUT_SECONDS

logger = logging.getLogger(__name__)

# argparse.Namespace is not a pydantic type, so allow it through validation.
_validate = validate_call(config=ConfigDict(arbitrary_types_allowed=True))


# Ordered venue_type groups for `paperpush venues`, mirroring the README's "Supported
# venues" section. Each tuple is (venue_type value, human-facing heading); a
# group with no venues still prints ("none yet") so every supported kind stays
# visible.
_VENUE_GROUPS = [
    ("preprint", "Preprint servers"),
    ("journal", "Journals"),
    ("conference", "Conferences"),
]

_VENUES_URL = "https://github.com/pachterlab/paperpush/blob/main/venues.md"

# Agent-facing docs shipped inside the package (see package-data in
# pyproject.toml). Mirrored from the repo root by scripts/sync_agent_docs.py so
# a pip install -- which gets no AGENTS.md -- can still read the contract.
_DOCS_DIR = Path(__file__).with_name("_docs")
_AGENT_GUIDE_PATH = _DOCS_DIR / "agents.md"
_AGENT_GUIDE_URL = "https://github.com/pachterlab/paperpush/blob/main/AGENTS.md"


@_validate
def _print_agent_guide() -> int:
    """Print the agent-facing guide (the packaged copy of AGENTS.md).

    Exists so an AI agent driving a pip-installed paperpush can read the
    submission contract without network access: `--help` points here, and this
    reads the copy that shipped with the installed version rather than whatever
    the GitHub HEAD happens to say.
    """
    try:
        print(_AGENT_GUIDE_PATH.read_text(encoding="utf-8"), end="")
    except OSError as exc:
        print(f"error: could not read the packaged agent guide: {exc}", file=sys.stderr)
        print(f"Read it online instead: {_AGENT_GUIDE_URL}", file=sys.stderr)
        return 1
    return 0


_VALIDATE_ONLY = "paperpush can check a manuscript against its author guidelines ('paperpush validate') but cannot submit to it, so there is nothing to log in to."


_TYPE_LABELS = {venue_type: heading.removesuffix("s").removesuffix(" server") for venue_type, heading in _VENUE_GROUPS}
_CHECK = "\u2713"


def _parse_keywords(text: str) -> list[str]:
    """Split a ``--keywords`` value into known keywords, or raise ValueError naming the bad ones."""
    from difflib import get_close_matches

    from .database import VENUE_KEYWORDS

    wanted = [k.strip().lower() for k in text.split(",") if k.strip()]
    if not wanted:
        raise ValueError("--keywords needs at least one keyword")
    problems = []
    for k in wanted:
        if k not in VENUE_KEYWORDS:
            close = get_close_matches(k, VENUE_KEYWORDS, n=1)
            problems.append(f"unknown keyword {k!r}" + (f" (did you mean {close[0]!r}?)" if close else ""))
    if problems:
        raise ValueError("; ".join(problems) + ". Known keywords: " + ", ".join(VENUE_KEYWORDS))
    return wanted


# Columns of the `paperpush venues` table, in display order: the names --columns and
# --hide-columns take.
_VENUE_COLUMNS = ("slug", "venue", "keywords", "type", "link", "validate", "submit")


def _parse_columns(text: str, flag: str) -> list[str]:
    """Split a ``--columns``/``--hide-columns`` value into known column names, or raise ValueError."""
    from difflib import get_close_matches

    wanted = [c.strip().lower() for c in text.split(",") if c.strip()]
    if not wanted:
        raise ValueError(f"{flag} needs at least one column")
    problems = []
    for c in wanted:
        if c not in _VENUE_COLUMNS:
            close = get_close_matches(c, _VENUE_COLUMNS, n=1)
            problems.append(f"unknown column {c!r}" + (f" (did you mean {close[0]!r}?)" if close else ""))
    if problems:
        raise ValueError("; ".join(problems) + ". Columns: " + ", ".join(_VENUE_COLUMNS))
    return list(dict.fromkeys(wanted))


def _select_columns(columns: list[str] | None, hidden: list[str] | None) -> list[str]:
    """The columns to show: ``columns`` in the order given (default: all), minus ``hidden``."""
    shown = [c for c in (columns or _VENUE_COLUMNS) if c not in set(hidden or ())]
    if not shown:
        raise ValueError("no columns left to show")
    return shown


@_validate
def _print_venues(
    validate: bool = False,
    submit: bool = False,
    keywords: list[str] | None = None,
    types: list[str] | None = None,
    columns: list[str] | None = None,
) -> int:
    from rich.console import Console
    from rich.table import Table

    venues = list_venues()
    if not venues:
        print("No venues are configured.")
        return 0
    # Filters combine with AND; --keywords matches a venue carrying any of them.
    if validate:
        venues = [v for v in venues if v.supports_validate]
    if submit:
        venues = [v for v in venues if v.supports_submit]
    if keywords:
        venues = [v for v in venues if set(keywords) & set(v.keywords)]
    if types:
        venues = [v for v in venues if v.venue_type in types]
    if not venues:
        print("No venues match the given filters.")
        return 0

    # Group by venue_type in _VENUE_GROUPS order (unknown types last), then by name.
    order = {venue_type: i for i, (venue_type, _) in enumerate(_VENUE_GROUPS)}
    venues = sorted(venues, key=lambda v: (order.get(v.venue_type, len(order)), v.name.lower()))

    # name -> (header, rich column options, cell value for a venue)
    specs = {
        # Slug and name never truncate; keywords and link wrap instead when space is short.
        "slug": ("Slug", dict(style="cyan", no_wrap=True, min_width=max(len(v.slug) for v in venues)), lambda v: v.slug),
        "venue": ("Venue", dict(no_wrap=True, min_width=max(len(v.name) for v in venues)), lambda v: v.name),
        "keywords": ("Keywords", dict(ratio=1, min_width=12), lambda v: ", ".join(v.keywords)),
        "type": ("Type", dict(no_wrap=True), lambda v: _TYPE_LABELS.get(v.venue_type, v.venue_type.capitalize())),
        "link": ("Link", dict(style="dim", overflow="fold", ratio=1, min_width=12), lambda v: v.site_url or v.submission_url),
        "validate": ("Validate", dict(justify="center", style="green", no_wrap=True), lambda v: _CHECK if v.supports_validate else ""),
        "submit": ("Submit", dict(justify="center", style="green", no_wrap=True), lambda v: _CHECK if v.supports_submit else ""),
    }
    shown = list(columns or _VENUE_COLUMNS)
    table = Table(box=None, header_style="bold", pad_edge=False)
    for name in shown:
        header, options, _ = specs[name]
        table.add_column(header, **options)
    for venue in venues:
        table.add_row(*(specs[name][2](venue) for name in shown))

    console = Console()
    if not console.is_terminal:
        # Piped or captured: one row per line rather than squeezing to 80 columns.
        console = Console(width=max(console.width, 250))
    console.print(table)
    console.print(f"\nFor more details, see {_VENUES_URL}", highlight=False, soft_wrap=True)
    console.print(f"Venue data: {venue_data.active_source().describe()} (refresh with 'paperpush update-venues')", highlight=False, soft_wrap=True)
    return 0


@_validate
def _cmd_venues(args: argparse.Namespace) -> int:
    try:
        keywords = _parse_keywords(args.keywords) if args.keywords is not None else None
        columns = _select_columns(
            _parse_columns(args.columns, "--columns") if args.columns is not None else None,
            _parse_columns(args.hide_columns, "--hide-columns") if args.hide_columns is not None else None,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return _print_venues(validate=args.validate, submit=args.submit, keywords=keywords, types=args.types, columns=columns)


@_validate
def _cmd_recovery_report(args: argparse.Namespace) -> int:
    """Draft a GitHub issue from a submission run's recovery log.

    When a step of ``submit`` failed and was fixed in the browser, the run
    logged what failed and what worked. This turns that log into a redacted
    report (typed values, paths, e-mails, .sub values, and URL query strings
    removed) and a prefilled ``issues/new`` link. Nothing is sent: the author
    reads the draft and files it if they are happy to share it.
    """
    from .venues import recovery_report

    directory = Path(args.directory).expanduser() if args.directory else recovery_report.latest_run_with_recoveries()
    if directory is None:
        print(f"No submission run with recoveries found under {recovery_report.runs_root()}.", file=sys.stderr)
        print("Pass the run's recovery directory (printed by 'paperpush submit') explicitly.", file=sys.stderr)
        return 1
    sub_values = None
    if args.subfile:
        from .subfile import load

        try:
            sub_values = load(args.subfile).values
        except OSError as exc:
            print(f"error: cannot read {args.subfile}: {exc}", file=sys.stderr)
            return 1
    try:
        report = recovery_report.build_report(directory, sub_values=sub_values)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(report.as_dict(), indent=2))
        return 0
    print(f"# {report.title}\n")
    print(report.body)
    print("-" * 72)
    print("This draft has NOT been sent anywhere. Read it, remove anything you do not want to share,")
    print("then file it (you need a GitHub account):")
    print(f"\n  {report.url}\n")
    if report.truncated:
        print(f"The link carries a shortened body; paste the full report from {report.saved_to}.")
    elif report.saved_to:
        print(f"The report is also saved at {report.saved_to}.")
    if report.screenshots:
        print(f"Screenshots ({len(report.screenshots)}) are in {directory}. They show your manuscript, so attach one only if you are happy to.")
    if args.open:
        import webbrowser

        webbrowser.open(report.url)
    return 0


def _cmd_install_skills(args: argparse.Namespace) -> int:
    """Install, list, or remove the agent skills that ship with paperpush."""
    from . import skills as skills_mod

    shipped = skills_mod.available()
    if not shipped:
        print("error: this installation has no packaged skills; get them from " "https://github.com/pachterlab/paperpush/tree/main/skills", file=sys.stderr)
        return 1
    try:
        chosen = skills_mod.select(shipped, args.names, include_contributor=args.all or args.uninstall or args.list)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    directory = skills_mod.target_dir(args.agent, project=args.project, dest=args.dest)

    if args.list:
        for skill in chosen:
            state = "installed" if (directory / skill.name / "SKILL.md").is_file() else "not installed"
            kind = " (contributor; needs a source checkout)" if skill.contributor else ""
            print(f"{skill.name}{kind} [{state}]")
            if skill.description:
                print(textwrap.fill(skill.description, width=88, initial_indent="    ", subsequent_indent="    ", break_on_hyphens=False))
        print(f"\nSkills directory: {directory}")
        return 0

    if args.uninstall:
        results = skills_mod.uninstall(chosen, directory)
        for skill, outcome in results:
            if outcome == "removed":
                print(f"removed {skill.name}")
            elif outcome == "kept":
                print(f"kept {skill.name}: {directory / skill.name} was not installed by this command")
        if not any(o == "removed" for _, o in results):
            print(f"No paperpush skills to remove in {directory}.")
        return 0

    results = skills_mod.install(chosen, directory, overwrite=args.overwrite)
    for skill, outcome in results:
        if outcome == "skipped":
            print(f"skipped {skill.name}: {directory / skill.name} already exists and was not installed by this command (--overwrite replaces it)")
        else:
            print(f"{outcome} {skill.name}")
    print(f"Skills directory: {directory}")
    if not args.all and not args.names:
        left_out = [s.name for s in shipped if s.contributor]
        if left_out:
            print(f"Contributor skills not installed ({', '.join(left_out)}); add --all if you work on paperpush itself.")
    if any(o in ("installed", "updated") for _, o in results):
        print("Start a new agent session to load them.")
    return 1 if any(o == "skipped" for _, o in results) else 0


def _cmd_update_venues(args: argparse.Namespace) -> int:
    """Fetch the published venue data now, or clear the cached copy.

    The data is otherwise refreshed on its own at most once a day; this is for
    picking up a fix right away, and for seeing which copy is in use.
    """
    from .database import reload

    if args.clear:
        venue_data.clear_cache()
        print(f"Removed the cached venue data; using the data {venue_data.bundled_source().describe()}.")
        return 0

    if not venue_data.uses_published():
        print("note: published venue data is not used here (PAPERPUSH_VENUE_DATA is set); fetching it into the cache anyway.", file=sys.stderr)
    try:
        result = venue_data.refresh(force=True)
    except Exception as exc:
        print(f"error: could not fetch the venue data from {venue_data.data_url()}: {exc}", file=sys.stderr)
        return 1
    reload()
    print(result.message if result.status != "incompatible" else f"warning: {result.message}")
    source = venue_data.active_source()
    print(f"In use: {source.describe()}")
    if source.kind != "bundled":
        print(f"Files: {source.root}")
    return 0


@_validate
def _cmd_add_venue(args: argparse.Namespace) -> int:
    """Write a venue file to fill in (``--template``), or merge a filled one into the databases."""
    from . import venuefile

    if args.template:
        slug = (args.file or "").strip()
        if slug.endswith(".txt"):
            print(f"error: with --template, give the new venue's slug (e.g. 'my_journal'), not a file name; use -o {slug} to name the file", file=sys.stderr)
            return 2
        if slug and not venuefile.SLUG_RE.match(slug):
            print(f"error: {slug!r} is not a valid slug: use lowercase letters, digits and underscores", file=sys.stderr)
            return 2
        out = args.output or f"{slug or args.from_slug or 'venue'}.txt"
        try:
            text = venuefile.template(slug, from_slug=args.from_slug, filename=Path(out).name if out != "-" else "VENUE.txt")
        except KeyError:
            print(f"error: unknown venue '{args.from_slug}'", file=sys.stderr)
            print("Run 'paperpush venues' to see supported venues.", file=sys.stderr)
            return 2
        if out == "-":
            sys.stdout.write(text)
            return 0
        path = Path(out)
        if path.exists() and not args.overwrite:
            print(f"error: {path} already exists (use --overwrite to replace it)", file=sys.stderr)
            return 1
        path.write_text(text, encoding="utf-8")
        what = f"{args.from_slug}'s current entries" if args.from_slug else "the fields most portals share"
        print(f"Wrote {path}, starting from {what}.")
        print("Fill it in from the venue's submission portal and author guidelines (by hand, or ask an AI agent),")
        print(f"then run:  paperpush add-venue {path}" + (" --overwrite" if args.from_slug and (not slug or slug == args.from_slug) else ""))
        return 0

    if not args.file:
        print("error: give a venue file to add, or --template to write one", file=sys.stderr)
        return 2
    path = Path(args.file)
    try:
        doc = venuefile.load(path)
    except OSError as exc:
        print(f"error: cannot read {path}: {exc}", file=sys.stderr)
        return 1
    except venuefile.VenueFileError as exc:
        print(f"error: {path} cannot be read:", file=sys.stderr)
        for problem in exc.problems:
            print(f"  {path.name}: {problem}", file=sys.stderr)
        return 1

    data_dir = Path(args.data_dir) if args.data_dir else None
    result = venuefile.check(doc, force=args.overwrite, data_dir=data_dir)
    for warning in result.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    if result.errors:
        print(f"error: {path} does not describe a venue paperpush can load:", file=sys.stderr)
        for error in result.errors:
            print(f"  - {error}", file=sys.stderr)
        return 1

    if args.dry_run:
        print(json.dumps(venuefile.doc_to_json(doc), indent=2, ensure_ascii=False))
        print(f"\n(dry run) {path} is valid; nothing written.", file=sys.stderr)
        return 0

    merged = venuefile.merge(doc, data_dir=data_dir, force=args.overwrite)
    where = venuefile.database_paths(data_dir)["venues"].parent
    for name in merged.written:
        print(f"Added {doc.slug} to {where / name}")
    for name in merged.replaced:
        print(f"Replaced {doc.slug} in {where / name}")
    for name in merged.kept:
        print(f"Kept {doc.slug}'s existing entry in {where / name} (the file leaves that part out)")

    print("\nNext:")
    print(f"  paperpush subfile {doc.slug}          # the .sub for the new venue")
    if doc.requirements is not None:
        print(f"  paperpush requirements {doc.slug}     # the guideline rules as validate reads them")
    if data_dir:
        print(f"  Use it with PAPERPUSH_VENUE_DATA={data_dir} (otherwise paperpush reads its own copy).")
    elif venue_data._in_source_checkout():
        print("  python scripts/gen_subfile_templates.py && python tests/sample_subfiles.py && python scripts/gen_readme_venues.py")
        if doc.form is not None and doc.requirements is not None:
            print(f"  python scripts/check_venue_consistency.py {doc.slug}")
        if doc.form is not None:
            print(f"  To submit: add a runner at paperpush/venues/<portal>/{doc.slug}.py (see CONTRIBUTING.md)")
        if venue_data.uses_published():
            print("  Set PAPERPUSH_VENUE_DATA=bundled while you work on it, so the published venue data does not override your entries.")
    else:
        print(f"  The entries are in the installed package ({where}); upgrading paperpush replaces them.")
        print(f"  To share the venue, open a pull request or issue with {path.name} at {__url__}")
        if venue_data.active_source().kind == "remote":
            print("  The published venue data in use does not have this venue yet: set PAPERPUSH_VENUE_DATA=bundled to use your entries.")
    return 0


@_validate
def _open_web(sub_path: Path, *, manuscript_dirs=(), action: str | None = None, colorblind: bool = False) -> int:
    """Open ``sub_path`` in the paperpush web page on this machine (``--web``) and serve it until Ctrl-C."""
    try:
        from .web.app import serve_local
    except ImportError:
        print("error: --web needs the web extra: pip install 'paperpush[web]'", file=sys.stderr)
        return 1
    return serve_local(Path(sub_path), manuscript_dirs=tuple(manuscript_dirs), action=action, **({"colorblind": True} if colorblind else {}))


# How long a preview server started by 'subfile'/'autofill' waits for a request
# before stopping by itself (an open page pings it every minute).
_PREVIEW_IDLE_SECONDS = 30 * 60


def _stdout_is_terminal() -> bool:
    return sys.stdout.isatty()


def _print_web_link(sub_path: Path, *, manuscript_dirs=(), blank_line: bool = False) -> None:
    """Print a localhost link that shows ``sub_path`` in the web page, served in the background.

    Starts ``paperpush web FILE`` as a detached process that stops once
    nobody has used the page for a while, so the command itself still returns
    at once. Skipped quietly when stdout is not a terminal (scripts, tests, the
    MCP server), when the web extra is not installed, or when
    ``PAPERPUSH_NO_WEB_LINK`` is set.
    """
    import importlib.util

    if os.environ.get("PAPERPUSH_NO_WEB_LINK") or not _stdout_is_terminal():
        return
    if blank_line:
        print()
    if importlib.util.find_spec("fastapi") is None or importlib.util.find_spec("uvicorn") is None:
        print("  (pip install 'paperpush[web]' to get a link that shows it in your browser)")
        return
    import secrets
    import socket
    import subprocess  # nosec B404

    from .subfile import parse

    try:
        venue = parse(sub_path.read_text(encoding="utf-8")).venue or subfile_stem(sub_path)
    except (OSError, UnicodeDecodeError):
        return
    host = "127.0.0.1"
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        port = sock.getsockname()[1]
    token = secrets.token_urlsafe(24)
    cmd = [sys.executable, "-m", "paperpush", "web", str(sub_path.resolve()), "--no-browser", "--port", str(port), "--idle-timeout", str(_PREVIEW_IDLE_SECONDS)]
    for d in manuscript_dirs:
        cmd += ["-p", str(Path(d).resolve())]
    try:
        subprocess.Popen(  # nosec B603
            cmd,
            env={**os.environ, "PAPERPUSH_WEB_TOKEN": token},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        logger.warning("could not start the preview server: %s", exc)
        return
    from .web.app import local_url

    url = local_url(host, port, token, venue)
    print(f"  View and edit it in your browser: {url}")
    print(f"  (served from this computer; edits save to {sub_path}; stops after {_PREVIEW_IDLE_SECONDS // 60} min unused)")


def _cmd_web(args: argparse.Namespace) -> int:
    """``paperpush web [FILE.sub.txt]``: serve the web page on this machine, optionally with a .sub open in it.

    Runs the page locally rather than pointing at the hosted site, so it opens
    at once (no cold start) and the .sub's files never leave this computer.
    """
    try:
        from .web.app import serve_app, serve_local
    except ImportError:
        print("error: 'paperpush web' needs the web extra: pip install 'paperpush[web]'", file=sys.stderr)
        return 1
    open_browser = not args.no_browser
    if not args.subfile:
        if args.paths:
            print("error: --path needs a .sub file to go with it", file=sys.stderr)
            return 2
        return serve_app(port=args.port, open_browser=open_browser)
    path = Path(args.subfile)
    if not path.is_file():
        print(f"error: {path} does not exist (create it with 'paperpush subfile VENUE --web')", file=sys.stderr)
        return 1
    from .subfile import parse

    try:
        venue = parse(path.read_text(encoding="utf-8")).venue
    except (OSError, UnicodeDecodeError) as exc:
        print(f"error: cannot read {path}: {exc}", file=sys.stderr)
        return 1
    if not venue:
        print(f"error: {path} has no '@venue:' line, so it is not a .sub file", file=sys.stderr)
        return 1
    given = [Path(p).expanduser() for p in args.paths or ()]
    missing = [p for p in given if not p.exists()]
    if missing:
        print(f"error: no such file or directory: {', '.join(map(str, missing))}", file=sys.stderr)
        return 1
    # A file stands for the directory holding it: the pickers list directories.
    dirs = tuple(dict.fromkeys(p if p.is_dir() else p.parent for p in given))
    return serve_local(
        path,
        manuscript_dirs=dirs,
        port=args.port,
        open_browser=open_browser,
        token=os.environ.get("PAPERPUSH_WEB_TOKEN") or None,
        idle_timeout=args.idle_timeout,
    )


@_validate
def _cmd_pick_venue(args: argparse.Namespace) -> int:
    from .autofill import AutofillApiError
    from .venue_select import format_hits, pick_venues, rank_via_api, select

    positional = list(args.keywords_pos or [])
    manuscript = args.manuscript
    # A single argument naming an existing file is the manuscript, not a keyword.
    if not manuscript and len(positional) == 1 and Path(positional[0]).expanduser().is_file():
        manuscript, positional = str(Path(positional[0]).expanduser()), []
    # Positional words form phrases; commas separate keywords ("single cell, T cell exhaustion").
    keywords = ([" ".join(positional)] if positional else []) + list(args.keywords or [])
    name = args.venue_name or ""
    if not (keywords or args.title or args.abstract or manuscript or name or args.types or args.portal):
        print("error: pick-venue needs keywords, --title, --abstract, --manuscript, --venue-name, --type, or --portal", file=sys.stderr)
        print("Run 'paperpush pick-venue --help' for usage.", file=sys.stderr)
        return 2

    try:
        selection = select(keywords=keywords, title=args.title or "", abstract=args.abstract or "", manuscript=manuscript, name=name, venue_types=tuple(args.types or ()), portals=tuple(args.portal or ()))
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    mq = selection.manuscript
    if mq is not None and mq.is_empty():
        print(f"error: found no title, abstract, or text to go on in {mq.source}", file=sys.stderr)
        return 1

    ranking = None
    if args.ai:
        if mq is None:
            print("error: --ai needs something to judge: keywords, --title, --abstract, or --manuscript", file=sys.stderr)
            return 2
        judged = mq
        candidates = [h.venue for h in pick_venues("", venue_types=tuple(args.types or ()), portals=tuple(args.portal or ()))]
        limit = len(candidates) if args.all else max(args.limit, 1)
        try:
            ranking = rank_via_api(judged, candidates, limit=limit, provider=args.provider, model=args.model)
        except AutofillApiError as exc:
            print(f"error: --ai: {exc}", file=sys.stderr)
            return 1

    if args.json:
        data = selection.to_dict(limit=None if args.all else args.limit)
        data["ai"] = ranking.to_dict() if ranking else None
        json.dump(data, sys.stdout, indent=2)
        print()
        return 0 if (selection.hits or (ranking and ranking.picks)) else 1

    if mq is not None and mq.source not in ("query", "options"):
        found = [name for name, value in (("title", mq.title), ("abstract", mq.abstract), ("keywords", mq.keywords)) if value]
        if found:
            print(f"Read the {' and '.join(found) if len(found) < 3 else 'title, abstract, and keywords'} from {mq.source}.")
        else:
            print(f"No title or abstract found in {mq.source}; used the start of its text.")
        if mq.title:
            print(f"  Title: {mq.title}")
    if ranking is not None:
        if ranking.subjects:
            print(f"Subjects ({ranking.model}): {', '.join(ranking.subjects)}")
        if not ranking.picks:
            print("error: the model picked no venues", file=sys.stderr)
            return 1
        by_slug = {h.venue.slug: h for h in pick_venues("", venues=candidates)}
        shown = [by_slug[p.slug] for p in ranking.picks]
        reasons = {p.slug: f"{p.fit}: {p.reason}" for p in ranking.picks}
        for line in format_hits(shown, reasons=reasons):
            print(line)
        print(f"\nUse 'paperpush subfile {ranking.picks[0].slug}' to create its submission template.")
        return 0

    if selection.inferred:
        print("Subjects inferred: " + ", ".join(f"{s.subject} ({', '.join(s.terms[:3])})" for s in selection.inferred))
    elif mq is not None and not selection.subjects and not name:
        print(f"error: could not tell the subject; add --keywords with one or more venue subjects ({', '.join(k for k in _venue_keywords() if k != 'general')}) or use --ai", file=sys.stderr)
        return 1

    hits = selection.hits
    if not hits:
        # Reaching here with no hits means the input matched nothing -- an
        # unusable answer for both humans and agents, hence a failing exit code
        # rather than an empty list.
        what = f" the venue name {name!r}" if name and mq is None else " the given input"
        print(f"error: no venues matched{what}", file=sys.stderr)
        print("Run 'paperpush venues' to see supported venues.", file=sys.stderr)
        return 1

    shown = hits if args.all else hits[: max(args.limit, 0)]
    for line in format_hits(shown, show_keywords=bool(selection.subjects)):
        print(line)

    # The subfile hint always names the top-ranked hit, even when --limit
    # hides it, so a scripted run stays actionable with a single glance.
    print(f"\nUse 'paperpush subfile {hits[0].venue.slug}' to create its submission template.")
    if mq is not None:
        print("This matched words against each venue's subject keywords; add --ai for an LLM's ranking with reasons.")
    return 0


def _venue_keywords() -> tuple[str, ...]:
    from .database import VENUE_KEYWORDS

    return VENUE_KEYWORDS


def _cmd_subfile(args: argparse.Namespace) -> int:
    logger.debug("subfile: requested venue %r, output=%r, fill_defaults=%s, " "overwrite=%s, venue_order=%s", args.venue, args.output, args.fill_defaults, args.overwrite, getattr(args, "venue_order", False))
    try:
        venue = get_venue(args.venue)
    except KeyError:
        logger.warning("subfile: unknown venue %r", args.venue)
        print(f"error: unknown venue '{args.venue}'", file=sys.stderr)
        print("Run 'paperpush venues' to see supported venues.", file=sys.stderr)
        return 2

    if args.output:
        target = with_subfile_suffix(args.output)
    else:
        target = Path(default_filename(venue))
    try:
        path = write_template(
            venue,
            path=target,
            fill_defaults=args.fill_defaults,
            overwrite=args.overwrite,
            venue_order=getattr(args, "venue_order", False),
        )
    except FileExistsError:
        if getattr(args, "web", False):
            # Nothing to create: open the existing file instead.
            print(f"{target} already exists; opening it (use --overwrite to start from a fresh template).")
            return _open_web(target)
        logger.info("subfile: refusing to overwrite existing %s", target)
        print(f"error: {target} already exists (use --overwrite to replace it)", file=sys.stderr)
        return 1

    required = sum(1 for f in venue.fields if f.required)
    logger.info("subfile: wrote template %s for %s (%d fields, %d required)", path, venue.slug, len(venue.fields), required)
    print(f"Created {path} for {venue.slug}.")
    if getattr(args, "web", False):
        print(f"  {len(venue.fields)} fields ({required} required).")
        return _open_web(Path(path))
    print(f"  {len(venue.fields)} fields ({required} required). " "Open it in your editor and fill in the values.")
    _print_web_link(Path(path))
    return 0


def _login_args(venue_slug: str) -> argparse.Namespace:
    """Build the default ``login`` arguments for an interactive sign-in.

    Used by ``submit`` to drive ``_cmd_login`` directly when no credentials are
    stored: a plain username/password login (prompting as needed), with the
    ORCID, status, logout, and ``--into`` paths left off. An author who wants to
    sign in through ORCID runs ``paperpush login --orcid <venue>`` first; submit
    then finds that credential stored and never reaches here.
    """
    return argparse.Namespace(
        venue=venue_slug,
        username=None,
        password=None,
        confirm_password=False,
        orcid=False,
        orcid_id=None,
        into=None,
        status=False,
        logout=False,
        list=False,
        # submit drives its own browser sign-in next, so don't open a second
        # verification browser here.
        no_verify=True,
        verify_headless=False,
        timeout=DEFAULT_TIMEOUT_SECONDS,
    )


def _describe_identity(cred) -> str:
    """One-line description of who a stored credential signs in as.

    Spells out the sign-in method for an ORCID credential (and the iD versus the
    registered email, since either may have been given), so ``--list`` and
    ``--status`` read the same way.
    """
    if cred.method != "orcid":
        return cred.username
    who = f"ORCID iD {cred.orcid}" if cred.orcid else f"ORCID account {cred.username}"
    return f"{who} ({cred.display_name})" if cred.display_name else who


def _list_logins() -> int:
    """Print every venue with stored credentials, and who they belong to."""
    creds = credentials.list_credentials()
    if not creds:
        print("Not logged in to any venues.")
        return 0
    from . import venues

    print("Logged in to:")
    for cred in creds:
        who = _describe_identity(cred)
        # A venue that submits through this one's portal shares this single
        # login, so list each sibling on its own line as if separately logged
        # in -- the credential is stored once under the base slug, but the user
        # thinks in terms of the journal they submit to.
        for slug in [cred.venue, *venues.submission_aliases(cred.venue)]:
            print(f"  {slug}: {who}")
    return 0


def _check_login(venue, args: argparse.Namespace) -> int:
    """Re-verify the stored credential for ``venue`` against its real sign-in.

    ``--status`` only says a credential is stored; this signs in with it, so a
    password changed since ``paperpush login`` is caught before ``submit``. Runs
    headless (no window, no prompt) so an agent can call it unattended -- which
    means a CAPTCHA or two-factor step cannot be completed and reads as a failure.

    Exit codes: 0 sign-in works (or the venue cannot be checked automatically --
    a note says so), 1 nothing stored or the sign-in failed.
    """
    from .venues.login import LoginVerificationError, verify_login

    cred = credentials.get_credential(venue.slug)
    if cred is None:
        print(f"Not logged in to {venue.slug}. Run 'paperpush login {venue.slug}' first.")
        return 1

    who = _describe_identity(cred)
    print(f"Checking the stored {venue.slug} login ({who}) by signing in…")
    try:
        verify_login(
            venue.slug,
            cred.username,
            cred.password,
            method=cred.method,
            headless=True,
            timeout=getattr(args, "timeout", DEFAULT_TIMEOUT_SECONDS),
        )
    except NotImplementedError as exc:
        logger.warning("login --check: cannot drive %s sign-in for %s: %s", cred.method, venue.slug, exc)
        print(f"Credentials are stored for {venue.slug}, but they could not be checked: {exc}")
        return 0
    except LoginVerificationError as exc:
        reason = str(exc)
        if "no automated sign-in" in reason:
            logger.info("login --check: %s cannot be checked automatically: %s", venue.slug, reason)
            print(f"Credentials are stored for {venue.slug}, but they could not be checked: {reason}")
            return 0
        logger.warning("login --check: stored credentials for %s failed: %s", venue.slug, reason)
        print(f"error: the stored {venue.slug} login did not work: {reason}", file=sys.stderr)
        print(
            f"Run 'paperpush login {venue.slug}' to sign in again (it opens a browser, " "so a CAPTCHA or two-factor prompt can be completed by hand).",
            file=sys.stderr,
        )
        return 1
    print(f"Sign-in confirmed: the stored {venue.slug} login works.")
    return 0


@_validate
def _cmd_login(args: argparse.Namespace) -> int:
    if getattr(args, "list", False):
        return _list_logins()

    if not args.venue:
        print("error: a venue is required (or use --list)", file=sys.stderr)
        print("Run 'paperpush venues' to see supported venues.", file=sys.stderr)
        return 2

    logger.debug("login: venue=%r orcid=%s status=%s logout=%s", args.venue, bool(args.orcid or args.orcid_id), args.status, args.logout)
    try:
        venue = get_venue(args.venue)
    except KeyError:
        logger.warning("login: unknown venue %r", args.venue)
        print(f"error: unknown venue '{args.venue}'", file=sys.stderr)
        print("Run 'paperpush venues' to see supported venues.", file=sys.stderr)
        return 2

    # A venue that submits through another's portal (e.g. the AAAS family)
    # shares that base venue's account, so its credentials live under the base
    # slug. Redirect to the base so storage, status, and logout all act on the one
    # shared login. (Schema inheritance is separate -- see submission_base.)
    from . import venues

    if not venue.has_form:
        print(f"{venue.slug} is validate-only: {_VALIDATE_ONLY}")
        return 0

    sub_base = venues.submission_base(venue.slug)
    if sub_base != venue.slug:
        print(f"{venue.slug} shares the {sub_base} sign-in; storing under '{sub_base}'.")
        venue = get_venue(sub_base)

    if not venues.login_required(venue.slug):
        print(f"{venue.slug} does not require login; submissions start directly in its portal.")
        return 0

    if args.logout:
        if credentials.delete_credential(venue.slug):
            logger.info("login: removed stored credentials for %s", venue.slug)
            print(f"Removed stored credentials for {venue.slug}.")
        else:
            logger.info("login: no stored credentials to remove for %s", venue.slug)
            print(f"No stored credentials for {venue.slug}.")
        return 0

    if args.status:
        cred = credentials.get_credential(venue.slug)
        if cred is None:
            print(f"Not logged in to {venue.slug}.")
            return 1
        location = credentials.credential_location(venue.slug)
        store = "OS keyring" if location == "keyring" else "config file"
        who = _describe_identity(cred)
        if cred.method == "orcid":
            print(f"Logged in to {venue.slug} via {who} (stored in {store}).")
        else:
            print(f"Logged in to {venue.slug} as {who} (stored in {store}).")
        return 0

    if getattr(args, "check", False):
        return _check_login(venue, args)

    if args.orcid or args.orcid_id:
        # ORCID sign-in is a journal thing: the preprint servers and conference
        # portals have their own accounts and no ORCID button to click.
        if not venues.orcid_login_offered(venue.slug):
            logger.info("login: %s does not offer ORCID sign-in", venue.slug)
            print(f"error: {venue.slug} does not offer signing in with ORCID.", file=sys.stderr)
            print(f"Sign in with a {venue.slug} username and password instead: " f"'paperpush login {venue.slug}'.", file=sys.stderr)
            return 2
        return _login_orcid(venue, args)

    # Collect the username (flag, then env var, then prompt).
    username = args.username or os.environ.get("PAPERPUSH_USERNAME")
    if not username:
        try:
            username = input(f"{venue.slug} username or email: ").strip()
        except EOFError:
            username = ""
    if not username:
        print("error: a username is required", file=sys.stderr)
        return 1

    # Collect the password (flag, then env var, else prompt once -- twice with
    # --confirm-password). The flag is a convenience for non-interactive use;
    # warn because it exposes the password.
    password = args.password
    if password:
        print("warning: --password exposes the password as plain text in the " "process list and shell history", file=sys.stderr)
    else:
        password = os.environ.get("PAPERPUSH_PASSWORD")
    if not password:
        try:
            password = getpass.getpass("Password: ")
            if getattr(args, "confirm_password", False):
                confirm = getpass.getpass("Confirm password: ")
                if password != confirm:
                    print("error: passwords do not match", file=sys.stderr)
                    return 1
        except EOFError:
            print("error: no password provided", file=sys.stderr)
            return 1
    if not password:
        print("error: a password is required", file=sys.stderr)
        return 1

    # Verify the credentials against the venue's real sign-in before storing
    # them, so a typo is caught now rather than at submit time. --no-verify skips
    # this (and the browser launch it needs).
    if not getattr(args, "no_verify", False):
        from .venues.login import LoginVerificationError, verify_login

        print(f"Checking the credentials by signing in to {venue.slug}…")
        try:
            verify_login(venue.slug, username, password, headless=getattr(args, "verify_headless", False), timeout=getattr(args, "timeout", DEFAULT_TIMEOUT_SECONDS))
        except LoginVerificationError as exc:
            logger.warning("login: verification failed for %s: %s", venue.slug, exc)
            print(f"error: sign-in check failed: {exc}", file=sys.stderr)
            print("Credentials were not stored. Re-run with --no-verify to store " "them without checking.", file=sys.stderr)
            return 1
        print("Sign-in confirmed.")

    used_keyring = credentials.save_credential(venue.slug, username, password)
    logger.info("login: stored password credentials for %s (keyring=%s)", venue.slug, used_keyring)
    print(f"Stored credentials for {venue.slug} (user: {username}).")
    if used_keyring:
        print("  Saved to the operating system secret store.")
    else:
        path = credentials.config_dir() / "credentials.json"
        print(f"  Saved to {path} (readable only by you).")
        print("  Note: no usable OS secret store was found, so this fallback " "file is not encrypted.")
    if venue.submission_url:
        print(f"  Submission system: {venue.submission_url}")
    return 0


def _login_orcid(venue, args: argparse.Namespace) -> int:
    """Store an ORCID sign-in for a venue: the author's ORCID iD and password.

    The ORCID branch of :func:`_cmd_login`, and deliberately the same shape as
    the username/password one: collect a credential (an ORCID iD or registered
    email plus the ORCID password), check it against the venue's real sign-in --
    the same page, taken down its "Sign in with ORCID" path -- and store it. What
    differs is only which form the browser fills, so ``submit`` later signs in
    through ORCID rather than the venue's own account.

    With ``--into`` the author's public ORCID record is read afterwards and their
    ORCID (plus any blank email/affiliation) written into a .sub author block.
    That lookup is a convenience, not part of signing in: it never happens
    without the flag, and a failure there does not fail the login.
    """
    from . import orcid as orcid_mod

    # Collect the ORCID iD (flag, then env var, then prompt) -- the same order
    # the username/password path uses.
    orcid_id = args.orcid_id or os.environ.get("PAPERPUSH_ORCID_ID") or os.environ.get("PAPERPUSH_USERNAME")
    if not orcid_id:
        try:
            orcid_id = input("ORCID iD or email: ").strip()
        except EOFError:
            orcid_id = ""
    if not orcid_id:
        print("error: an ORCID iD is required", file=sys.stderr)
        return 1
    if not orcid_mod.is_valid_identity(orcid_id):
        print(f"error: '{orcid_id}' is not a valid ORCID iD or email address", file=sys.stderr)
        print("An ORCID iD looks like 0000-0002-1825-0097.", file=sys.stderr)
        return 1
    orcid_id = orcid_mod.normalize_identity(orcid_id)

    # And the ORCID password, on the same terms as the venue password: the flag
    # warns, the env var is silent, otherwise prompt once (twice with
    # --confirm-password).
    password = args.password
    if password:
        print("warning: --password exposes the password as plain text in the " "process list and shell history", file=sys.stderr)
    else:
        password = os.environ.get("PAPERPUSH_PASSWORD")
    if not password:
        try:
            password = getpass.getpass("ORCID password: ")
            if getattr(args, "confirm_password", False):
                confirm = getpass.getpass("Confirm ORCID password: ")
                if password != confirm:
                    print("error: passwords do not match", file=sys.stderr)
                    return 1
        except EOFError:
            print("error: no password provided", file=sys.stderr)
            return 1
    if not password:
        print("error: a password is required", file=sys.stderr)
        return 1

    # Verify against the venue's real sign-in before storing, exactly as the
    # username/password path does -- but down the ORCID branch of the same page.
    if not getattr(args, "no_verify", False):
        from .venues.login import LoginVerificationError, verify_login

        print(f"Checking the ORCID credentials by signing in to {venue.slug}…")
        try:
            verify_login(
                venue.slug,
                orcid_id,
                password,
                method="orcid",
                headless=getattr(args, "verify_headless", False),
                timeout=getattr(args, "timeout", DEFAULT_TIMEOUT_SECONDS),
            )
        except NotImplementedError as exc:
            # The venue offers ORCID sign-in but paperpush cannot drive it yet.
            # Storing the pair would only defer the failure to submit time.
            logger.warning("login --orcid: no automated ORCID sign-in for %s: %s", venue.slug, exc)
            print(f"error: {exc}", file=sys.stderr)
            return 1
        except LoginVerificationError as exc:
            logger.warning("login --orcid: verification failed for %s: %s", venue.slug, exc)
            print(f"error: ORCID sign-in check failed: {exc}", file=sys.stderr)
            print("Credentials were not stored. Re-run with --no-verify to store " "them without checking.", file=sys.stderr)
            return 1
        print("Sign-in confirmed.")

    # --into reads the public record; do it before storing so the author's name
    # can be recorded alongside the credential.
    profile = None
    if args.into:
        if orcid_mod.is_valid_id(orcid_id):
            try:
                profile = orcid_mod.fetch_profile(orcid_id)
            except orcid_mod.OrcidError as exc:
                logger.warning("login --orcid: could not read public record for %s: %s", orcid_id, exc)
                print(f"warning: could not read the public ORCID record: {exc}", file=sys.stderr)
        else:
            print("warning: --into needs an ORCID iD (an email cannot be looked " "up in the public registry); skipping.", file=sys.stderr)

    used_keyring = credentials.save_orcid_credential(venue.slug, orcid_id, password, name=profile.name if profile else "")
    logger.info("login --orcid: stored ORCID login for %s (iD %s, keyring=%s)", venue.slug, orcid_id, used_keyring)

    who = orcid_id + (f" ({profile.name})" if profile and profile.name else "")
    print(f"Stored ORCID login for {venue.slug} (iD: {who}).")
    if used_keyring:
        print("  Saved to the operating system secret store.")
    else:
        path = credentials.config_dir() / "credentials.json"
        print(f"  Saved to {path} (readable only by you).")
        print("  Note: no usable OS secret store was found, so this fallback " "file is not encrypted.")
    if profile and profile.affiliation:
        print(f"  Affiliation: {profile.affiliation}")
    if venue.submission_url:
        print(f"  Submission system: {venue.submission_url}")

    if profile is not None:
        _populate_orcid_into(args.into, venue, profile)
    return 0


def _populate_orcid_into(sub_path: str, venue, profile) -> None:
    """Fill the authenticated author's ORCID details into a .sub author block."""
    from . import orcid as orcid_mod
    from .subfile import find_block, replace_block

    author_field = next((f for f in venue.fields if f.type == "authorlist"), None)
    if author_field is None:
        print(f"  Skipped --into: {venue.slug} has no author list field.")
        return

    path = Path(sub_path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"  warning: could not open {sub_path}: {exc}", file=sys.stderr)
        return

    block = find_block(text, author_field.id)
    if block is None:
        print(f"  warning: no '{author_field.id}' block found in {sub_path}.", file=sys.stderr)
        return

    new_block, matched = orcid_mod.fill_author_block(block, profile, author_field.fields)
    if matched is None:
        print(f"  warning: could not match an author line to ORCID iD " f"{profile.orcid_id} in {sub_path}.", file=sys.stderr)
        return
    if new_block == block:
        print(f"  {sub_path}: author '{matched}' already has these details.")
        return

    path.write_text(replace_block(text, author_field.id, new_block), encoding="utf-8")
    print(f"  Updated {sub_path}: filled ORCID details for author '{matched}'.")


def _report_validation(subfile, venue_def, subfile_path: str, *, check_sensitive: bool = True, check_links: bool = True, check_references: bool = True, check_manuscript: bool = True, check_anonymous: bool | None = None, check_hidden_text: bool = True, check_openreview: bool = True) -> list:
    """Validate a loaded .sub against its venue and print the findings.

    Runs the same checks ``submit`` performs before opening a browser --
    required/missing fields, files that must exist, allowed options, file types,
    and booleans -- printing warnings (advisory) and an error summary (blocking)
    to stderr. Returns the list of error :class:`~paperpush.validate.Issue`
    objects (empty when the file passes), so the caller decides what to do next.
    Shared by ``submit`` (the gate before submission) and the standalone
    ``validate`` command.

    When ``check_links`` is set (the default), the URLs cited in the referenced
    files are probed and broken ones reported. When ``check_references`` is set
    (also the default), the DOIs in the submission's bibliography are resolved
    and compared against what each entry claims; both make network requests.
    When ``check_sensitive`` is set, those files are additionally scanned for
    information not meant to be published (secrets, GPS metadata,
    editable-document links, LaTeX comments); all surface as advisory warnings.
    When ``check_manuscript`` is set (the default), the manuscript and other
    uploads are measured against the venue's author guidelines
    (``manuscript_requirements.json``): formats, length, required sections and
    statements, title-page items, figure resolution, reference count.
    ``check_anonymous`` (default: the venue's ``anonymous`` flag) scans the
    uploads and any linked anonymous.4open.science repositories for information
    identifying the authors. ``check_hidden_text`` looks for text a reader cannot
    see (hidden prompts to AI reviewers are errors); ``check_openreview`` looks
    up the authors' OpenReview profiles for OpenReview venues.
    """
    from .validate import validate

    issues = validate(
        subfile,
        venue_def,
        check_sensitive=check_sensitive,
        check_links=check_links,
        check_references=check_references,
        check_manuscript=check_manuscript,
        check_anonymous=check_anonymous,
        check_hidden_text=check_hidden_text,
        check_openreview=check_openreview,
    )
    errors = [i for i in issues if i.is_error]
    warnings = [i for i in issues if not i.is_error]
    for issue in warnings:
        where = f"[{issue.field}] " if issue.field else ""
        print(f"warning: {where}{issue.message}", file=sys.stderr)
    if errors:
        logger.warning("validation failed for %s: %d error(s)", venue_def.slug, len(errors))
        print(f"\nerror: {len(errors)} problem(s) in {subfile_path} must be " "fixed before submitting:", file=sys.stderr)
        for issue in errors:
            where = f"[{issue.field}] " if issue.field else ""
            print(f"  - {where}{issue.message}", file=sys.stderr)
    return errors


def _clean_latex_sources(subfile, venue_def):
    """Swap the .sub's LaTeX uploads for arxiv_latex_cleaner'd copies (``--arxiv-latex-cleaner``).

    Runs :func:`paperpush.latex_cleaner.clean_values` and prints where each
    cleaned copy was written. The .sub on disk is left alone; the returned
    :class:`~paperpush.subfile.SubFile` points at the copies so the rest of
    ``validate`` checks what would be uploaded. Returns None (after printing
    an error) when the optional package is not installed.
    """
    from . import latex_cleaner
    from .subfile import SubFile

    if not latex_cleaner.available():
        print(f"error: --arxiv-latex-cleaner needs the arxiv_latex_cleaner package; install it with `{latex_cleaner.INSTALL_HINT}`", file=sys.stderr)
        return None
    values, cleaned, failures = latex_cleaner.clean_values(venue_def, subfile.values)
    for field_id, path, reason in failures:
        print(f"warning: [{field_id}] arxiv_latex_cleaner could not clean {path}: {reason}; validating the original", file=sys.stderr)
    if not cleaned and not failures:
        print("note: --arxiv-latex-cleaner found no LaTeX source (.tex, or a .zip/.tar bundle with one) to clean", file=sys.stderr)
    for c in cleaned:
        print(f"note: [{c.field_id}] cleaned {c.original} -> {c.cleaned}", file=sys.stderr)
    if cleaned:
        print("note: validating the cleaned copies; point the .sub at them to submit them", file=sys.stderr)
    return SubFile(venue=subfile.venue, values=values)


# The checks, as listed at the end of 'paperpush validate --help'. Keep in step
# with docs/validation-checks.rst and "What it checks" in docs/commands/validate.rst.
_VALIDATE_CHECKS = """\
checks that run without AI:
  submission form   required fields and confirmations, valid options, types,
                    item counts, word/character limits, URLs, author list
  files             files exist, accepted types and sizes, total upload size,
                    portal word/page limits
  integrity         reachable links, reference DOIs, secrets and private data,
                    hidden text / prompt injection, anonymity (double-blind),
                    OpenReview profiles
  guidelines        formats and sizes, word/page limits, title/abstract/keyword
                    limits, undefined abbreviations in the abstract, required
                    sections and their order, required statements, title-page
                    items, separate title page, reference count, citation style
                    (numbered or author-year, numbered in order), DOIs on every
                    reference, preprints/unpublished work, figure/table/
                    supplementary counts, combined supplementary PDF, cover
                    letter, edits to a mandatory template
  layout            template or LaTeX class, page size, margins, body font and
                    size, double spacing, line numbers, page numbers, English
                    spelling variant
  figures           resolution (dpi, line-art threshold), lettering size and
                    typeface in vector figures, panel labels (case, bold, cited
                    panels present, labelled panels cited), colour mode, print size, placement,
                    legend length and location -- attached figure files, or the
                    figures embedded in the manuscript when none are attached
  citation order    main, supplementary, and Extended Data figures and tables
                    first cited in order, all cited, none cited that is missing;
                    panels of each figure and table (every series) first
                    cited in order
  .sub vs paper     title, abstract, authors (names, order, emails, ORCIDs),
                    keywords, and statements match the manuscript word for word
  front matter      no citations in the abstract, structured abstract headings
                    (when the venue asks for them), a contact e-mail address
  references        every reference cited and every citation listed, share of
                    self-citations (>15, or >80% of the list)
  prose             double spaces mid-sentence, repeated words ("the the"),
                    spacing around punctuation, unbalanced brackets and quotes,
                    common misspellings (codespell), repetitive word choice

checks that need AI (--ai, or an agent via --ai-tasks / --ai-results; add
--ai-only to run just these):
  lettering size, typeface, and panel labels in raster figures, and that
  each of their labelled panels is cited; title-page
  items found only by reading (postal address, author footnotes, one-sentence
  summary, classification, affiliations); tables pasted as images; reference
  formatting in the venue's style; template used, for a PDF or Word manuscript;
  the venue's free-text guideline rules; whether every .sub field agrees with
  the manuscript in meaning

Guideline rules no check reads are listed as 'not implemented'. See
'paperpush requirements VENUE' for a venue's rules.
"""


@_validate
def _cmd_validate(args: argparse.Namespace) -> int:
    """Run the pre-submission checks on a .sub file and print the full report.

    Loads the .sub file, looks up the venue it declares, runs every check, and
    prints a colour-coded report of what passed, what failed, what could not be
    checked (needs AI, not implemented, or could not run here), and why. With
    ``--ai`` the checks only an LLM can judge go to a provider's API; with
    ``--ai-results`` they are taken from an agent's verdicts, and
    ``--ai-tasks`` writes those tasks out for the agent; ``--ai-only`` reports
    just those checks. ``-o`` saves the report
    (PDF by default; .html, .md, or .txt by extension). Exits 0 when the file is
    ready to submit, 1 when it has errors, and 2 when the venue is unknown.
    """
    from . import report as report_mod
    from .subfile import load

    logger.debug("validate: subfile=%r", args.subfile)
    try:
        subfile = load(args.subfile)
    except OSError as exc:
        logger.error("validate: cannot read %s: %s", args.subfile, exc)
        print(f"error: cannot read {args.subfile}: {exc}", file=sys.stderr)
        return 1

    venue = subfile.venue or "biorxiv"
    logger.info("validate: loaded %s targeting %s (%d values)", args.subfile, venue, len(subfile.values))

    try:
        venue_def = get_venue(venue)
    except KeyError:
        logger.error("validate: unknown venue %r in %s", venue, args.subfile)
        print(f"error: unknown venue '{venue}'", file=sys.stderr)
        print("Run 'paperpush venues' to see supported venues.", file=sys.stderr)
        return 2

    ai_only = getattr(args, "ai_only", False)
    if ai_only and not getattr(args, "check_manuscript", True):
        print("error: --ai-only cannot be combined with --dont-check-manuscript: the manuscript checks decide which AI checks apply", file=sys.stderr)
        return 1
    if ai_only and getattr(args, "web", False):
        print("error: --ai-only is not supported with --web", file=sys.stderr)
        return 1

    if getattr(args, "web", False):
        action = "validate-ai" if getattr(args, "ai", False) else "validate"
        if getattr(args, "initial_submission", False):
            action += "+initial"
        return _open_web(Path(args.subfile), action=action, colorblind=getattr(args, "alt_color", False))

    report_out = None
    if getattr(args, "output", None) is not None:
        report_out = report_mod.output_path(args.output, args.subfile)
        if report_out.exists() and not getattr(args, "overwrite", False):
            print(f"error: {report_out} already exists (use --overwrite to replace it)", file=sys.stderr)
            return 1

    if getattr(args, "arxiv_latex_cleaner", False):
        subfile = _clean_latex_sources(subfile, venue_def)
        if subfile is None:
            return 1

    ai = None
    if getattr(args, "ai_results", None):
        from .ai_validate import AiOptions

        try:
            data = json.loads(Path(args.ai_results).expanduser().read_text(encoding="utf-8"))
            ai = AiOptions.from_results(data, args.ai_results)
        except (OSError, ValueError) as exc:
            print(f"error: cannot read the AI results in {args.ai_results}: {exc}", file=sys.stderr)
            return 1
    elif getattr(args, "ai", False):
        from .ai_validate import AiOptions

        ai = AiOptions.api(provider=args.provider, model=args.model)

    with _validate_progress_bar() as progress:
        result = validate_report_for(
            subfile,
            venue_def,
            args.subfile,
            ai=ai,
            progress=progress,
            check_sensitive=getattr(args, "check_sensitive", True),
            check_links=getattr(args, "check_links", True),
            check_references=getattr(args, "check_references", True),
            check_manuscript=getattr(args, "check_manuscript", True),
            check_anonymous=getattr(args, "check_anonymous", None),
            check_hidden_text=getattr(args, "check_hidden_text", True),
            check_openreview=getattr(args, "check_openreview", True),
            initial_submission=getattr(args, "initial_submission", False),
            cover_letter_max_age_days=getattr(args, "cover_letter_max_age", 7),
            ai_only=ai_only,
        )
    alt = getattr(args, "alt_color", False)
    report_mod.print_report(result, alt_color=alt)

    if getattr(args, "ai_tasks", None):
        from .ai_validate import tasks_payload

        tasks_path = Path(args.ai_tasks).expanduser()
        tasks_path.write_text(json.dumps(tasks_payload(result.ai_tasks, venue_def, args.subfile), indent=2) + "\n", encoding="utf-8")
        again = " --ai-only" if ai_only else ""
        print(f"\nWrote {len(result.ai_tasks)} AI check(s) to {tasks_path}; have an agent answer them, then run 'paperpush validate {args.subfile}{again} --ai-results <results.json>'.")
    if report_out is not None:
        out = report_out
        try:
            report_mod.save(result, out, alt_color=alt)
        except OSError as exc:
            print(f"error: could not save the report to {out}: {exc}", file=sys.stderr)
            return 1
        print(f"\nSaved the report to {out}")

    if result.errors:
        print("\nFix the errors above, then run 'paperpush validate' again.", file=sys.stderr)
        return 1
    if ai_only:
        print(f"\nRan only the AI checks on {args.subfile}; run 'paperpush validate' without --ai-only for the full pre-submission checks.")
        return 0
    if result.downgraded:
        print(f"\n{args.subfile} passed validation for {venue} as an initial submission ({result.downgraded} error(s) shown as warnings); ready to submit.")
    else:
        print(f"\n{args.subfile} passed validation for {venue}; ready to submit.")
    return 0


@contextlib.contextmanager
def _validate_progress_bar():
    """A ``progress`` callback for :func:`validate_report` drawing a tqdm bar on stderr.

    Yields None (no bar) when tqdm is not installed (``pip install
    'paperpush[validate]'``) or stderr is not a terminal, so piped and logged
    output stays clean. The bar is cleared when validation ends, before the
    report prints.
    """
    try:
        from tqdm import tqdm
    except ImportError:
        yield None
        return
    if not sys.stderr.isatty():
        yield None
        return
    bars: list = []

    def progress(done: int, total: int, label: str) -> None:
        if not bars:
            bars.append(tqdm(total=total, unit="stage", leave=False, file=sys.stderr, bar_format="Validating {percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt} {desc}"))
        bar = bars[0]
        bar.total = total
        bar.n = done
        bar.set_description_str("" if label == "done" else f"· {label}", refresh=False)
        bar.refresh()

    try:
        yield progress
    finally:
        for bar in bars:
            bar.close()


def validate_report_for(subfile, venue_def, subfile_path: str, *, ai=None, **flags):
    """:func:`paperpush.validate.validate_report` for the CLI (a seam for tests)."""
    from .validate import validate_report

    return validate_report(subfile, venue_def, ai=ai, subfile_path=subfile_path, **flags)


@_validate
def _cmd_submit(args: argparse.Namespace) -> int:
    """Open the venue's portal and drive the submission wizard from a .sub file.

    Loads the .sub file, picks the runner for the venue it declares, opens a
    browser, signs in (reusing a saved session where supported, else by hand),
    then clicks through the wizard typing in the field values from the file. It
    stops before the final submit so you can review.
    """
    from . import venues
    from .subfile import load

    logger.debug("submit: subfile=%r headless=%s debug=%s new_session=%s", args.subfile, args.headless, args.debug, args.new_session)
    try:
        subfile = load(args.subfile)
    except OSError as exc:
        logger.error("submit: cannot read %s: %s", args.subfile, exc)
        print(f"error: cannot read {args.subfile}: {exc}", file=sys.stderr)
        return 1

    venue = subfile.venue or "biorxiv"
    logger.info("submit: loaded %s targeting %s (%d values)", args.subfile, venue, len(subfile.values))

    # Check the filled-in file against the venue's field schema before opening
    # a browser: required/missing fields, files that must exist, allowed options,
    # file types, and booleans. Errors block; warnings are advisory. Skip if the
    # venue is unknown -- the runner lookup below reports that.
    try:
        venue_def = get_venue(venue)
    except KeyError:
        venue_def = None
    if venue_def is not None and not venue_def.has_form:
        print(f"error: {venue_def.slug} is validate-only: {_VALIDATE_ONLY}", file=sys.stderr)
        return 1
    if venue_def is not None and getattr(args, "skip_validate", False):
        logger.info("submit: --skip-validate given; not validating %s", args.subfile)
        print(f"warning: --skip-validate given; submitting {args.subfile} without running validate.", file=sys.stderr)
    elif venue_def is not None:
        errors = _report_validation(subfile, venue_def, args.subfile)
        if errors:
            print(
                "\nFix the items above, then run 'paperpush submit' again "
                "(or pass --skip-validate to submit without validating).",
                file=sys.stderr,
            )
            return 1

    try:
        run = venues.get_runner(venue)
    except KeyError:
        logger.error("submit: no submission runner registered for %r", venue)
        print(f"error: no submission runner for venue '{venue}'.", file=sys.stderr)
        return 1

    # Credentials and the saved session are keyed on the submission-base venue,
    # so an alias submission (e.g. a Science Advances .sub) signs in with the
    # shared Science login rather than a separate, never-created one.
    cred_slug = venues.submission_base(venue_def.slug if venue_def is not None else venue)

    # If no credentials are stored, run the login flow now so the submission can
    # sign in unattended. The user may still decline (e.g. to sign in by hand in
    # the browser), so a failed or skipped login is advisory, not blocking.
    needs_login = venues.login_required(cred_slug)
    env_creds = bool(os.environ.get("PAPERPUSH_USERNAME") and os.environ.get("PAPERPUSH_PASSWORD"))
    if needs_login and credentials.get_credential(cred_slug) is None and not sys.stdin.isatty() and not env_creds:
        # No terminal (CI, `</dev/null`, the MCP server's silent pipe) and no
        # credentials in the environment: the login prompt would block forever
        # or, worse, read stdin a caller meant for something else -- a CI loop's
        # list of .sub files, say. Rely on the saved session alone; without one
        # the run stops with auth_required.
        logger.info("submit: no stored credentials for %s and no terminal; skipping the login prompt", cred_slug)
        print(f"No stored credentials for {cred_slug} and no terminal to log in from; relying on a saved session " f"(run 'paperpush login {cred_slug}' in a terminal if there is none).")
    elif needs_login and credentials.get_credential(cred_slug) is None:
        logger.info("submit: no stored credentials for %s; prompting to log in", cred_slug)
        print(f"Not logged in to {cred_slug}; starting login (Ctrl-C to skip and " "sign in by hand in the browser).")
        try:
            _cmd_login(_login_args(cred_slug))
        except (KeyboardInterrupt, EOFError):
            print()
            logger.info("submit: login skipped for %s", cred_slug)
        if credentials.get_credential(cred_slug) is None:
            print(f"Continuing without stored credentials for {cred_slug}; you can " "sign in by hand in the browser.")

    if args.debug:
        print(f"Opening {venue} in debug mode for {args.subfile}…")
        if needs_login:
            print("The Playwright Inspector opens at the first step; use 'Step " "over' to walk the wizard line by line. If a saved session " "exists (from an earlier submit run) you start signed in; " "otherwise sign in in the browser, then resume.")
        else:
            print("The Playwright Inspector opens at the first step; use 'Step " "over' to walk the wizard line by line. This public portal " "does not require sign-in.")
    elif needs_login:
        print(f"Opening {venue} to run the submission for {args.subfile}…")
        print("Sign-in is automatic when possible: a saved session is reused, " f"else your 'paperpush login {venue}' credentials are " "filled in (and the session saved); otherwise sign in by hand. " "Field values come from the .sub file; the wizard stops before " "the final submit.")
    else:
        print(f"Opening {venue} to run the submission for {args.subfile}…")
        print("This portal does not require login. Field values come from the " ".sub file; the wizard stops before the final submit.")
    logger.info("submit: launching %s runner (headless=%s, debug=%s, timeout=%ss, " "keep_open_on_failure=%s)", venue, args.headless, args.debug, args.timeout, not args.close_on_failure)
    try:
        handler, cdp_port = _submit_recovery_setup(args, venue, cred_slug, subfile.values)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    from .venues.common import SignInRequired
    from .venues.recovery import use_handler
    from .venues.recovery_channel import use_cdp_port

    try:
        with use_handler(handler), use_cdp_port(cdp_port):
            run(subfile.values, headless=args.headless, debug=args.debug, new_session=args.new_session, timeout=args.timeout, keep_open_on_failure=not args.close_on_failure)
    except SignInRequired as exc:
        logger.error("submit: %s", exc)
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        _print_recovery_report_hint(handler)
    logger.info("submit: %s runner returned", venue)
    return 0


def _print_recovery_report_hint(handler) -> None:
    """After a run that needed recovery, say how to report it so the venue gets fixed."""
    from .venues.recovery_channel import read_log, recovery_dir_of

    directory = recovery_dir_of(handler)
    if directory is None:
        return
    entries = read_log(directory)
    if not entries:
        return
    print(
        f"\n{len(entries)} step(s) of this run needed recovery. The venue script may be out of date for everyone.\n"
        f"To help fix it, review a redacted report and file it if you are happy to share it:\n"
        f"  paperpush recovery-report {directory}",
        file=sys.stderr,
    )


def _submit_recovery_setup(args: argparse.Namespace, venue: str, base_slug: str, values: dict):
    """Build the recovery handler and CDP port that ``submit``'s flags ask for.

    Returns ``(handler, cdp_port)``; either may be ``None``. Raises
    :class:`ValueError` for a combination that cannot work.
    """
    from . import venues
    from .venues.recovery import HumanHandler
    from .venues.recovery_channel import AgentHandler, RememberingHandler, allowed_upload_paths, cdp_endpoint, free_port, recovery_dir_of
    from .venues.recovery_report import write_run_info

    mode = getattr(args, "recover", None)
    if getattr(args, "ai", False):
        if mode not in (None, "ai"):
            raise ValueError(f"--ai and --recover {mode} ask for different things; pick one")
        mode = "ai"
    mode = mode or "off"
    if mode != "ai" and (getattr(args, "provider", None) or getattr(args, "model", None)):
        raise ValueError("--provider and --model only apply with --ai")
    port = getattr(args, "cdp_port", None)
    if mode == "off" and port is None:
        return None, None

    try:
        impl = venues.get_venue_impl(base_slug)
    except (KeyError, ImportError):
        impl = None
    supported = bool(getattr(impl, "supports_recovery", False))
    if not supported:
        print(f"note: {venue}'s runner does not support recovery yet; --recover and --cdp-port have no effect.", file=sys.stderr)
        return None, None

    if args.recover_dir and mode not in ("agent", "ai", "by-hand"):
        raise ValueError("--recover-dir only applies with --recover agent, by-hand, or --ai")
    handler = None
    if mode == "by-hand":
        if args.headless:
            raise ValueError("--recover by-hand needs a visible browser to fix the page in; drop --headless")
        directory = Path(args.recover_dir) if args.recover_dir else _default_recover_dir(venue)
        subfile = getattr(args, "subfile", None)
        handler = HumanHandler(log_dir=directory, on_first_log=lambda: write_run_info(directory, venue, base_slug, subfile))
    elif mode == "agent":
        directory = Path(args.recover_dir) if args.recover_dir else _default_recover_dir(venue)
        agent = AgentHandler(directory, allowed_uploads=allowed_upload_paths(values), final_submit_names=getattr(impl, "final_submit_names", ()))
        handler = RememberingHandler(agent, agent.tools)
        write_run_info(directory, venue, base_slug, getattr(args, "subfile", None))
        print(f"Agent recovery is on: a failed step pauses the run for an agent working through {directory}.")
    elif mode == "ai":
        handler = _ai_recovery_handler(args, venue, impl, values)
        write_run_info(recovery_dir_of(handler), venue, base_slug, getattr(args, "subfile", None))

    if port is not None:
        if port < 0 or port > 65535:
            raise ValueError(f"--cdp-port must be between 0 and 65535, not {port}")
        port = port or free_port()
        print(f"CDP endpoint: {cdp_endpoint(port)}  (attach with: playwright-cli attach --cdp={cdp_endpoint(port)})")
        print("  Anything on this machine can drive the signed-in browser through it while the run is open.")
    return handler, port


def _ai_recovery_handler(args: argparse.Namespace, venue: str, impl, values: dict):
    """The ``--ai`` handler: the model first, then (in a terminal, with a window) the person."""
    import importlib.util

    from .autofill import _SDK_PACKAGES, DEFAULT_MODELS, AutofillApiError, resolve_provider
    from .venues.recovery import FallbackHandler, HumanHandler
    from .venues.recovery_ai import ApiAgentHandler
    from .venues.recovery_channel import PageTools, RememberingHandler, allowed_upload_paths

    try:
        provider = resolve_provider(getattr(args, "provider", None), alternative="use --recover by-hand instead")
    except AutofillApiError as exc:
        raise ValueError(f"--ai: {exc}") from None
    module = {"anthropic": "anthropic", "openai": "openai", "google": "google.genai"}[provider]
    try:
        found = importlib.util.find_spec(module) is not None
    except ModuleNotFoundError:
        found = False
    if not found:
        raise ValueError(f"--ai with {provider} needs the {_SDK_PACKAGES[provider]} package; install it with 'pip install paperpush[ai-{provider}]'")
    model = getattr(args, "model", None) or DEFAULT_MODELS[provider]

    directory = Path(args.recover_dir) if args.recover_dir else _default_recover_dir(venue)
    tools = PageTools(directory, allowed_uploads=allowed_upload_paths(values), final_submit_names=getattr(impl, "final_submit_names", ()))
    handler = RememberingHandler(ApiAgentHandler(tools, provider=provider, model=model), tools)
    print(f"AI recovery is on ({provider}, {model}). If a step fails, its page (the accessibility tree and a " "screenshot, which can show your manuscript details) is sent to the model so it can fix the step.")
    print(f"  Screenshots and a log of each fix go to {directory}")
    if not args.headless and sys.stdin.isatty():
        # What the author does after the model gives up goes in the same log.
        return FallbackHandler(handler, HumanHandler(log_dir=directory))
    return handler


def _default_recover_dir(venue: str) -> Path:
    """A fresh recovery directory for one ``submit --recover agent`` run."""
    from datetime import datetime

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    return credentials.config_dir() / "submit-runs" / f"{venue}-{stamp}"


# Document formats accepted as a direct ``autofill -d`` input (besides
# directories). Everything else -- figures, data -- is picked up by passing the
# directory that holds it.
AUTOFILL_INPUT_SUFFIXES = frozenset({".tex", ".ltx", ".doc", ".docx", ".odt", ".rtf", ".txt", ".md", ".markdown", ".rst", ".pdf"})


class _AutofillInputsAction(argparse.Action):
    """Collect ``-p`` inputs, stopping at the first ``.sub`` file.

    argparse hands an ``nargs="+"`` option every string up to the next flag, so
    ``-p ms/ notes.md biorxiv.sub.txt`` would swallow the .sub file. This action
    keeps the inputs before the first ``.sub`` and records that file as the
    subfile (reconciled with the positional in :func:`_cmd_autofill`). Repeated
    ``-p`` flags accumulate.
    """

    def __call__(self, parser, namespace, values, option_string=None):
        inputs = list(getattr(namespace, self.dest, None) or [])
        for i, value in enumerate(values):
            if is_subfile_name(value):
                if not inputs:
                    parser.error(f"{option_string}: expected at least one directory or document before {value}")
                if getattr(namespace, "_subfile_from_inputs", None):
                    parser.error(f"more than one .sub file given ({namespace._subfile_from_inputs}, {value})")
                namespace._subfile_from_inputs = value
                if values[i + 1 :]:
                    parser.error(f"unrecognized arguments after {value}: {' '.join(values[i + 1 :])}")
                break
            inputs.append(value)
        setattr(namespace, self.dest, inputs)


def _check_autofill_inputs(raw: list[str]) -> tuple[list[Path], list[Path]] | None:
    """Split ``-p`` inputs into ``(directories, files)``, or print why not.

    Every input must exist and be either a directory or a document in one of
    :data:`AUTOFILL_INPUT_SUFFIXES`. Returns None after printing an error.
    """
    dirs: list[Path] = []
    files: list[Path] = []
    for item in raw:
        path = Path(item).expanduser()
        if path.is_dir():
            dirs.append(path)
        elif path.is_file():
            if path.suffix.lower() not in AUTOFILL_INPUT_SUFFIXES:
                print(f"error: {path} is not a directory or a supported document " f"({', '.join(sorted(AUTOFILL_INPUT_SUFFIXES))})", file=sys.stderr)
                return None
            files.append(path)
        else:
            print(f"error: autofill input not found: {path}", file=sys.stderr)
            return None
    return dirs, files


@_validate
def _cmd_autofill(args: argparse.Namespace) -> int:
    """Populate a .sub file from values extracted from a manuscript directory.

    Loads (or, if absent, renders) the target .sub file, reads a set of proposed
    field values, and applies them through the shared autofill core: policy
    fields are left untouched, low-confidence and classified values are flagged
    for review, file paths are resolved against the manuscript directory, and
    the result is validated. The ``manual`` engine reads the proposals from a
    ``--values`` JSON file (this is the path the Claude skill drives); the
    ``api`` engine extracts them with an LLM API (Anthropic, OpenAI, or Google,
    chosen by ``--provider`` or by whichever API key is set).
    """
    from .autofill import autofill, parse_extraction
    from .subfile import parse, render_template

    if not args.inputs:
        # Not required=True in argparse, so that the hidden old name -d also satisfies it.
        parser = getattr(args, "_autofill_parser", None)
        if parser is not None:
            parser.error("the following arguments are required: -p/--path")
        print("error: the following arguments are required: -p/--path", file=sys.stderr)
        return 2
    trailing_sub = getattr(args, "_subfile_from_inputs", None)
    if trailing_sub and args.subfile:
        print(f"error: two .sub files given ({args.subfile}, {trailing_sub})", file=sys.stderr)
        return 2
    args.subfile = args.subfile or trailing_sub
    if not args.subfile:
        print("error: no .sub file given, e.g. 'paperpush autofill -p MANUSCRIPT_DIR biorxiv.sub.txt'", file=sys.stderr)
        return 2

    logger.debug(
        "autofill: subfile=%r inputs=%r engine=%s values=%r " "min_confidence=%s dry_run=%s output=%r",
        args.subfile,
        args.inputs,
        args.engine,
        args.values,
        args.min_confidence,
        args.dry_run,
        args.output,
    )

    checked = _check_autofill_inputs(args.inputs)
    if checked is None:
        return 1
    input_dirs, input_files = checked
    # Where relative file paths in the extracted values are looked up: the
    # given directories first, then the folders holding the given documents.
    search_dirs = list(dict.fromkeys([*input_dirs, *(f.parent for f in input_files)]))

    # --web without values to apply: the page does the extraction (with the
    # API key from the page or the environment), on the .sub file on disk.
    if getattr(args, "web", False) and args.engine == "manual" and not args.values:
        return _autofill_in_browser(args, input_dirs, input_files, search_dirs)

    if args.engine == "manual" and not args.values:
        print(
            "error: --values FILE is required with the manual engine.\n" "\n" "The manual engine applies field values that you extract yourself --\n" "it does not call any API. To produce them:\n" "  1. Run 'paperpush schema <venue>' to list the fields and roles.\n" "  2. Read the manuscript files and write a values.json (AGENTS.md has\n" "     the exact schema).\n" "  3. Re-run with --values values.json.\n" "Use '--engine api' instead only if an Anthropic, OpenAI, or Google API\n" "key is set and you want that API to do the extraction.",
            file=sys.stderr,
        )
        return 1

    # Load the target .sub if it exists; otherwise render a fresh template,
    # taking the venue slug from the filename stem (e.g. biorxiv.sub.txt).
    sub_path = Path(args.subfile)
    if sub_path.exists():
        try:
            text = sub_path.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"error: cannot read {sub_path}: {exc}", file=sys.stderr)
            return 1
        slug = parse(text).venue or subfile_stem(sub_path)
    else:
        slug = subfile_stem(sub_path)
        text = None

    try:
        venue = get_venue(slug)
    except KeyError:
        print(f"error: unknown venue '{slug}'", file=sys.stderr)
        print("Run 'paperpush venues' to see supported venues.", file=sys.stderr)
        return 2
    if text is None:
        text = render_template(venue, fill_defaults=True)
        print(f"{sub_path} does not exist; starting from a fresh {venue.slug} " "template.\n")

    # Obtain the proposed field values: from the API, or from a --values file.
    if args.engine == "api":
        extraction = _extract_with_api(venue, input_dirs, input_files, args)
        if extraction is None:
            return 1
    else:
        try:
            data = json.loads(Path(args.values).read_text(encoding="utf-8"))
        except OSError as exc:
            print(f"error: cannot read values file {args.values}: {exc}", file=sys.stderr)
            return 1
        except json.JSONDecodeError as exc:
            print(f"error: {args.values} is not valid JSON: {exc}", file=sys.stderr)
            return 1
        extraction = parse_extraction(data)

    result = autofill(text, venue, extraction, manuscript_dir=search_dirs, min_confidence=args.min_confidence)

    out_path = Path(args.output) if args.output else sub_path
    if args.dry_run:
        print(f"(dry run -- {out_path} not written)\n")
    else:
        if args.output and out_path.exists() and not args.overwrite and out_path.resolve() != sub_path.resolve():
            print(f"error: {out_path} already exists (use --overwrite to replace it)", file=sys.stderr)
            return 1
        out_path.write_text(result.text, encoding="utf-8")

    # autofill is a preparation step, not the final gate: it deliberately leaves
    # policy fields (e.g. author consent) empty, so residual validation errors
    # are expected and reported as guidance rather than a nonzero exit. 'submit'
    # is what blocks on them.
    _print_autofill_summary(result, extraction, venue, out_path, args.dry_run)
    if getattr(args, "web", False) and not args.dry_run:
        print()
        return _open_web(out_path, manuscript_dirs=search_dirs)
    if not args.dry_run:
        _print_web_link(out_path, manuscript_dirs=search_dirs, blank_line=True)
    return 0


def _autofill_in_browser(args: argparse.Namespace, input_dirs: list[Path], input_files: list[Path], search_dirs: list[Path]) -> int:
    """``autofill --web`` with nothing to apply here: open the page and let it run autofill.

    Creates the .sub (or --output) if it is missing, points its manuscript
    field at the manuscript found among the inputs when it is empty, and
    opens the page, which starts autofill straight away.
    """
    from .subfile import parse, render_template, replace_scalar

    try:
        from .web.app import manuscript_field
    except ImportError:
        print("error: --web needs the web extra: pip install 'paperpush[web]'", file=sys.stderr)
        return 1
    if args.dry_run:
        print("error: --dry-run cannot be combined with --web; the page shows each value before you keep it", file=sys.stderr)
        return 2
    sub_path = Path(args.subfile)
    out_path = Path(args.output) if args.output else sub_path
    if sub_path.exists():
        text = sub_path.read_text(encoding="utf-8")
        slug = parse(text).venue or subfile_stem(sub_path)
    else:
        text, slug = None, subfile_stem(sub_path)
    try:
        venue = get_venue(slug)
    except KeyError:
        print(f"error: unknown venue '{slug}'", file=sys.stderr)
        print("Run 'paperpush venues' to see supported venues.", file=sys.stderr)
        return 2
    if text is None:
        text = render_template(venue, fill_defaults=True)
    if args.output and out_path.exists() and not args.overwrite and out_path.resolve() != sub_path.resolve():
        print(f"error: {out_path} already exists (use --overwrite to replace it)", file=sys.stderr)
        return 1

    field = manuscript_field(venue)
    if field is not None and not parse(text).values.get(field.id, "").strip():
        candidates = list(input_files) + [c for d in input_dirs for c in sorted(d.iterdir()) if c.is_file()]
        manuscript = _find_document(candidates, search_dirs, args.manuscript, ("manuscript", "paper", "ms", "main"))
        if manuscript is None and input_files:
            manuscript = input_files[0]
        if manuscript is not None and manuscript.is_file():
            text = replace_scalar(text, field.id, str(manuscript))
    out_path.write_text(text, encoding="utf-8")
    return _open_web(out_path, manuscript_dirs=search_dirs, action="autofill")


def _print_autofill_summary(result, extraction, venue, out_path, dry_run: bool) -> None:
    """Print the filled / review / left-for-you breakdown after an autofill."""
    verb = "Would write" if dry_run else "Wrote"
    print(f"{verb} {out_path} for {venue.slug}.\n")

    if result.filled:
        print(f"Filled {len(result.filled)} field(s):")
        for o in result.filled:
            src = f"  ({o.source})" if o.source else ""
            print(f"  {o.id}{src}")

    if result.review:
        print(f"\n{len(result.review)} field(s) need your review:")
        for o in result.review:
            print(f"  {o.id}: {o.note}")

    policy = [o for o in result.skipped if o.action == "skipped_policy"]
    if policy or extraction.unfilled:
        print(f"\nLeft for you to set ({len(policy) + len(extraction.unfilled)}):")
        for o in policy:
            print(f"  {o.id} ({o.label})")
        for fid, reason in extraction.unfilled:
            print(f"  {fid}" + (f": {reason}" if reason else ""))

    low = [o for o in result.skipped if o.action == "skipped_low"]
    unknown = [o for o in result.skipped if o.action == "unknown"]
    for o in low:
        print(f"\nskipped {o.id}: {o.note}")
    for o in unknown:
        print(f"\nignored proposed field '{o.id}': {o.note}")

    if result.warnings:
        print(f"\n{len(result.warnings)} validation warning(s):")
        for issue in result.warnings:
            where = f"[{issue.field}] " if issue.field else ""
            print(f"  {where}{issue.message}")

    # Residual errors are expected (policy fields are intentionally left), so
    # they are guidance on what remains, printed to stdout to keep ordering.
    if result.errors:
        print(f"\n{len(result.errors)} field(s) still need filling in before " "submit:")
        for issue in result.errors:
            where = f"[{issue.field}] " if issue.field else ""
            print(f"  - {where}{issue.message}")
        print(f"\nFinish the items above in {out_path}, then run")
    else:
        print(f"\nNext: review {out_path}, then run")
    print(f"  paperpush login {venue.slug}")
    print(f"  paperpush submit {out_path}")


def _find_document(candidates: list[Path], search_dirs: list[Path], explicit: str | None, stems: tuple[str, ...]) -> Path | None:
    """Resolve a named document among the autofill inputs.

    An explicit path is honored (resolved against the first search directory
    that has it, if relative and not found as given); otherwise ``candidates``
    are searched in order for one whose stem matches any of ``stems``
    (case-insensitive). Returns None if nothing is found.
    """
    if explicit:
        p = Path(explicit)
        if not p.is_absolute() and not p.exists():
            for base in search_dirs:
                if (base / explicit).exists():
                    return base / explicit
        return p
    for child in candidates:
        if child.stem.lower() in stems:
            return child
    return None


def _list_inputs(dirs: list[Path], files: list[Path]) -> list[str]:
    """Paths of the files the model may assign to file fields.

    With a single directory and nothing else the paths are relative to it (as
    they always were); otherwise each is given as reachable from the working
    directory, so files from different inputs cannot be confused.
    """
    single = len(dirs) == 1 and not files
    out: list[str] = []
    for d in dirs:
        for path in sorted(d.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(d)
            if any(part.startswith((".", "__")) for part in rel.parts):
                continue
            out.append(str(rel) if single else str(path))
    out.extend(str(f) for f in files)
    return list(dict.fromkeys(out))


def _extract_with_api(venue, dirs: list[Path], files: list[Path], args: argparse.Namespace):
    """Build the document set and run the API extraction engine.

    Documents passed directly with ``-p`` are always sent; the manuscript,
    title page, and supplement are otherwise picked by name from those files
    and the top level of each directory. Returns an Extraction, or None after
    printing an error (so the caller can exit nonzero).
    """
    from .autofill import DEFAULT_MODELS, AutofillApiError, DocumentInput, detect_provider, extract_via_api

    search_dirs = list(dict.fromkeys([*dirs, *(f.parent for f in files)]))
    candidates = list(files) + [c for d in dirs for c in sorted(d.iterdir()) if c.is_file()]

    manuscript = _find_document(candidates, search_dirs, args.manuscript, ("manuscript", "paper", "ms", "main"))
    if manuscript is None and files:
        manuscript = files[0]  # the first document given directly
    if manuscript is None or not manuscript.is_file():
        print("error: could not find the manuscript; pass --manuscript <file> or the file itself with -d.", file=sys.stderr)
        return None

    documents = [DocumentInput("manuscript", manuscript)]
    title_page = _find_document(candidates, search_dirs, args.title_page, ("title_page", "titlepage", "title"))
    if title_page and title_page.is_file():
        documents.append(DocumentInput("title_page", title_page))
    supplement = _find_document(candidates, search_dirs, args.supplement, ("supplement", "supp", "si"))
    if supplement and supplement.is_file():
        documents.append(DocumentInput("supplement", supplement))
    used = {d.path.resolve() for d in documents}
    for f in files:
        if f.resolve() not in used:
            documents.append(DocumentInput("document", f))
            used.add(f.resolve())

    provider = args.provider or detect_provider()
    model = args.model or (DEFAULT_MODELS[provider] if provider else None)
    labels = ", ".join(f"{d.label}={d.path.name}" for d in documents)
    if provider:
        print(f"Reading {labels} via the {provider} API ({model})…")
    try:
        return extract_via_api(venue, documents, _list_inputs(dirs, files), model=model, provider=provider)
    except AutofillApiError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return None


@_validate
def _cmd_codegen(args: argparse.Namespace) -> int:
    """Record a venue's submission wizard as runner code (Playwright's codegen, for paperpush).

    Opens a browser with the paperpush recorder toolbar: record, play, pause,
    undo/redo, branches, step labels, .sub variables, copy the code, copy the
    page's HTML. Given a .sub (or --venue), it reuses the venue's saved sign-in,
    offers the venue's fields as variables, and binds typed values that match
    the .sub to their field automatically.
    """
    from .codegen.session import CodegenSession
    from .venues.common import session_path

    sub_values: dict[str, str] = {}
    slug = args.venue
    if args.sub:
        from .subfile import load

        try:
            subfile = load(args.sub)
        except OSError as exc:
            print(f"error: cannot read {args.sub}: {exc}", file=sys.stderr)
            return 1
        sub_values = {k: v for k, v in subfile.values.items() if v and v.strip()}
        slug = slug or subfile.venue or None

    fields: list[str] = list(sub_values)
    file_fields: list[str] = []
    field_options: dict[str, list[str]] = {}
    url = args.url or ""
    storage_state = None
    if slug:
        try:
            venue_def = get_venue(slug)
        except KeyError:
            print(f"error: unknown venue {slug!r} (see 'paperpush venues')", file=sys.stderr)
            return 1
        fields = [f.id for f in venue_def.fields]
        file_fields = [f.id for f in venue_def.fields if f.type in ("file", "filelist")]
        field_options = {f.id: [str(o) for o in f.options] for f in venue_def.fields if f.options}
        impl = None
        with contextlib.suppress(Exception):
            from .venues import get_venue_impl

            impl = get_venue_impl(slug)
        if not url:
            url = (impl.portal_url if impl is not None else "") or venue_def.submission_url
        session = impl.session_path() if impl is not None else session_path(slug)
        if not args.new_session and session.exists():
            storage_state = session
            print(f"Reusing the saved {slug} sign-in ({session}).")

    header = "Recorded with `paperpush codegen`" + (f" for {slug}" if slug else "") + "."
    rec = CodegenSession(url=url or "about:blank", target=args.target, output=args.output, sub_values=sub_values, fields=fields, file_fields=file_fields, field_options=field_options, storage_state=storage_state, header=header)
    print("Recording. Use the paperpush toolbar in the page; close the browser when you are done.")
    rendered = rec.run()
    if args.output:
        print(f"Wrote {args.output}")
    else:
        print(rendered.code)
    return 0


@_validate
def _cmd_schema(args: argparse.Namespace) -> int:
    """Print a venue's field schema, including autofill roles, as JSON.

    Used by the autofill front-ends (the Claude skill and the API engine) to
    learn which fields to extract, classify, map to files, or leave alone,
    without duplicating that knowledge outside ``submission_forms.json``.
    """
    from .autofill import field_schema

    try:
        venue = get_venue(args.venue)
    except KeyError:
        print(f"error: unknown venue '{args.venue}'", file=sys.stderr)
        print("Run 'paperpush venues' to see supported venues.", file=sys.stderr)
        return 2

    print(
        json.dumps(
            {
                "venue": venue.slug,
                "name": venue.name,
                "submission_guide": venue.submission_guide,
                "fields": field_schema(venue),
            },
            indent=2,
        )
    )
    return 0


@_validate
def _cmd_requirements(args: argparse.Namespace) -> int:
    """Print what a venue's author guidelines require of the manuscript.

    Reads ``manuscript_requirements.json`` -- the companion to ``submission_forms.json``
    that ``validate`` measures the uploads against -- and prints the venue's
    rules, either as a readable summary grouped by section (manuscript, title
    page, abstract, figures, ...) or as JSON with ``--json``. With
    ``--article-type`` the per-type overrides for that type are applied first.
    Exits 2 when the venue is unknown and 1 when no requirements are recorded.
    """
    from .requirements import get_requirements, resolve

    try:
        venue = get_venue(args.venue)
    except KeyError:
        print(f"error: unknown venue '{args.venue}'", file=sys.stderr)
        print("Run 'paperpush venues' to see supported venues.", file=sys.stderr)
        return 2
    reqs = get_requirements(venue.slug)
    if reqs is None:
        print(f"No manuscript requirements are recorded for {venue.slug}.", file=sys.stderr)
        return 1
    article_types = sorted(reqs.article_types)
    if args.article_type:
        if reqs.article_type_field:
            reqs = resolve(reqs, {reqs.article_type_field: args.article_type})
        if args.article_type not in article_types:
            print(f"warning: {venue.slug} records no article-type overrides for '{args.article_type}'; showing the default rules", file=sys.stderr)
    data = reqs.to_dict()
    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return 0

    print(f"Manuscript requirements for {venue.name} ({venue.slug})")
    if reqs.retrieved:
        print(f"  read from the author guidelines on {reqs.retrieved}")
    for url in reqs.source_urls:
        print(f"  {url}")
    if reqs.inherits:
        print(f"  (starts from {reqs.inherits}'s rules)")
    print(f"  venue data: {venue_data.active_source().describe()}")
    if article_types and not args.article_type:
        print(f"  article types with their own rules: {', '.join(article_types)} (use --article-type)")
    for section in ("manuscript", "title_page", "abstract", "keywords", "sections", "statements", "figures", "tables", "supplementary", "references", "cover_letter", "upload"):
        rules = data.get(section)
        if not rules:
            continue
        print(f"\n{section.replace('_', ' ')}:")
        notes = rules.pop("notes", [])
        for key, value in rules.items():
            if isinstance(value, list):
                value = ", ".join(str(v) for v in value)
            print(f"  {key.replace('_', ' ')}: {value}")
        for note in notes:
            print(f"  - {note}")
    if reqs.notes:
        print("\nnotes:")
        for note in reqs.notes:
            print(f"  - {note}")
    return 0


@_validate
def _cmd_options(args: argparse.Namespace) -> int:
    """Print the allowed values for a field, one per line.

    Targets a field as ``<venue>.<field>`` (e.g. ``arxiv.crosslist_categories``).
    Its main use is fields whose options come from a shared vocabulary file
    (``options_file``) too long to inline in the ``.sub`` template or an error
    message; it works for any field that defines options.

    A venue may register a custom lister for a field whose allowed values are not
    a flat list -- Nature's variable-depth subject tree, browsed one level at a
    time. Then trailing ``PATH`` args drill into the tree
    (``paperpush options nature.subject_level "Biological sciences"``).
    """
    from . import venues

    target = args.field
    venue_slug, _, field_id = target.partition(".")
    if not venue_slug or not field_id:
        print(f"error: expected VENUE.FIELD, got '{target}'", file=sys.stderr)
        print("Example: paperpush options arxiv.crosslist_categories", file=sys.stderr)
        return 2

    try:
        venue = get_venue(venue_slug)
    except KeyError:
        print(f"error: unknown venue '{venue_slug}'", file=sys.stderr)
        print("Run 'paperpush venues' to see supported venues.", file=sys.stderr)
        return 2

    field = next((f for f in venue.fields if f.id == field_id), None)
    if field is None:
        print(f"error: venue '{venue_slug}' has no field '{field_id}'", file=sys.stderr)
        return 2

    # A custom lister (e.g. Nature's subject tree) takes precedence and can drill
    # into a path; a bad path name raises ValueError, reported as a usage error.
    lister = venues.get_field_option_listers(venue_slug).get(field_id)
    if lister is not None:
        try:
            options = lister(*args.path)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        for option in options:
            print(option)
        return 0

    if args.path:
        print(f"error: field '{target}' is a flat list; it does not take a path", file=sys.stderr)
        return 2

    if not field.options:
        print(f"error: field '{target}' has no options to list", file=sys.stderr)
        return 1

    for option in field.options:
        print(option)
    return 0


def build_parser() -> argparse.ArgumentParser:
    # The epilog is the discovery path for AI agents: an agent handed a
    # pip-installed paperpush reaches for `--help` long before it would think to
    # look up a URL, so the pointer to the packaged guide belongs here.
    parser = argparse.ArgumentParser(
        prog="paperpush",
        description="Make venue submission as easy as a single click.",
        epilog=("AI agents: run 'paperpush --agent-guide' before driving this CLI.\n" f"Docs and issues: {__url__}"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"paperpush {__version__}")
    parser.add_argument("--agent-guide", dest="agent_guide", action="store_true", help="print the guide for AI agents driving this CLI (AGENTS.md) and exit")

    # Verbosity is a per-command concern, so -v/-q live on each subcommand
    # (added via this shared parent) rather than at the top level.
    verbosity = argparse.ArgumentParser(add_help=False)
    verbosity.add_argument("-v", "--verbose", action="count", default=0, help="increase logging verbosity (-v for info, " "-vv for debug); overridden by " "PAPERPUSH_LOG_LEVEL")
    verbosity.add_argument("-q", "--quiet", action="store_true", help="only log errors")

    # metavar lists only the public commands; the internal 'schema' command is
    # registered below but deliberately left out so it does not appear in --help.
    sub = parser.add_subparsers(dest="command", metavar="{venues,subfile,options,autofill,validate,requirements,login,submit,codegen,web,pick-venue,add-venue,update-venues,install-skills}")

    p_venues = sub.add_parser(
        "venues",
        parents=[verbosity],
        help="list the supported venues",
        description="List the venues paperpush supports, grouped by type. Filters combine: "
        "'paperpush venues --submit -t journal -k biology' lists biology journals paperpush can submit to.",
    )
    p_venues.add_argument("--validate", action="store_true", help="only venues paperpush can validate a manuscript against")
    p_venues.add_argument("--submit", action="store_true", help="only venues paperpush can submit to")
    p_venues.add_argument("-k", "--keywords", metavar="KW[,KW...]", help="only venues tagged with any of these comma-separated subject keywords (e.g. 'medicine,biology,bioinformatics')")
    p_venues.add_argument("-t", "--type", dest="types", action="append", choices=[t for t, _ in _VENUE_GROUPS], metavar="TYPE", help="only venues of this kind: preprint, journal, or conference (repeatable)")
    p_venues.add_argument("--columns", metavar="COL[,COL...]", help=f"show only these comma-separated columns, in this order (from: {', '.join(_VENUE_COLUMNS)})")
    p_venues.add_argument("--hide-columns", dest="hide_columns", metavar="COL[,COL...]", help="hide these comma-separated columns")
    p_venues.set_defaults(func=_cmd_venues)

    p_subfile = sub.add_parser("subfile", parents=[verbosity], help="create a <venue>.sub.txt submission template")
    p_subfile.add_argument("venue", help="venue slug, e.g. biorxiv")
    p_subfile.add_argument("-o", "--output", metavar="OUT", help="output path for the .sub file (default: VENUE.sub.txt in the current directory, e.g. biorxiv.sub.txt; .sub.txt is added unless the name already ends in .sub.txt or .sub)")
    p_subfile.add_argument("--dont-fill-defaults", dest="fill_defaults", action="store_false", help="leave fields with default values empty instead of pre-populating them")
    p_subfile.add_argument("--overwrite", action="store_true", help="overwrite an existing .sub file")
    p_subfile.add_argument("--force", dest="overwrite", action="store_true", help=argparse.SUPPRESS)
    p_subfile.add_argument("--web", action="store_true", help="then open the .sub in the paperpush web page on this computer, to fill it in through a form; edits are saved to the file (needs pip install 'paperpush[web]')")
    p_subfile.add_argument("--venue-order", dest="venue_order", action="store_true", help="list fields in the order the venue's submission form asks for them, instead of grouped into the sections every venue shares")
    p_subfile.set_defaults(func=_cmd_subfile, fill_defaults=True)

    p_options = sub.add_parser("options", parents=[verbosity], help="list the allowed values for a field (VENUE.FIELD)")
    p_options.add_argument("field", metavar="VENUE.FIELD", help="the field to list, e.g. arxiv.crosslist_categories")
    p_options.add_argument("path", nargs="*", metavar="PATH", help="for a drill-down field (e.g. nature.subject_level), the category " "names to descend before listing the next level")
    p_options.set_defaults(func=_cmd_options)

    p_autofill = sub.add_parser("autofill", parents=[verbosity], help="fill a .sub file from a directory of manuscript files")
    p_autofill.add_argument("subfile", nargs="?", help="the .sub file to fill (created from the filename's venue slug " "if it does not exist yet), e.g. biorxiv.sub.txt")
    p_autofill.add_argument(
        "-p",
        "--path",
        dest="inputs",
        nargs="+",
        action=_AutofillInputsAction,
        metavar="PATH",
        help="(required) one or more manuscript directories and/or document files (" + ", ".join(sorted(AUTOFILL_INPUT_SUFFIXES)) + "). " "Takes everything up to the next flag or the first .sub file, and may be repeated",
    )
    # Old names for -p/--path, kept working but left out of --help.
    p_autofill.add_argument("-d", "--directory", dest="inputs", nargs="+", action=_AutofillInputsAction, help=argparse.SUPPRESS)
    p_autofill.add_argument(
        "--engine",
        choices=["manual", "api"],
        default="manual",
        help="manual: read proposed values from --values (default; used by the " "Claude skill); api: extract them with an LLM API (see --provider)",
    )
    p_autofill.add_argument("--values", metavar="FILE", help="JSON file of proposed field values (required for --engine manual)")
    p_autofill.add_argument("--manuscript", metavar="FILE", help="(api) the manuscript file; inferred from the directory if omitted")
    p_autofill.add_argument("--title-page", dest="title_page", metavar="FILE", help="(api) a standalone title page with author details, if separate")
    p_autofill.add_argument("--supplement", metavar="FILE", help="(api) a supplementary materials file, if any")
    p_autofill.add_argument("--provider", choices=["anthropic", "openai", "google"], help="(api) LLM provider; defaults to the first of anthropic, openai, google " "whose API key is set (ANTHROPIC_API_KEY, OPENAI_API_KEY, GEMINI_API_KEY/GOOGLE_API_KEY)")
    p_autofill.add_argument("--model", help="(api) model to use (default: claude-opus-5-5, gpt-6-astra, or " "gemini-3.8-flash, by provider)")
    p_autofill.add_argument("--min-confidence", choices=["low", "medium", "high"], default="low", help="do not write any value below this confidence (default: low)")
    p_autofill.add_argument("-o", "--output", metavar="OUT", help="write the filled file here instead of overwriting the .sub")
    p_autofill.add_argument("--overwrite", action="store_true", help="overwrite --output if it already exists")
    p_autofill.add_argument("--force", dest="overwrite", action="store_true", help=argparse.SUPPRESS)
    p_autofill.add_argument("--dry-run", action="store_true", help="show what would be filled without writing the file")
    p_autofill.add_argument("--web", action="store_true", help="open the .sub in the paperpush web page on this computer to review the values; edits are saved to the file. Without --values or --engine api the page runs the autofill itself, with the API key from the page or the environment (needs pip install 'paperpush[web]')")
    p_autofill.set_defaults(func=_cmd_autofill, inputs=None, _autofill_parser=p_autofill)

    p_validate = sub.add_parser(
        "validate",
        parents=[verbosity],
        help="run the pre-submission checks on a .sub file",
        description="Run the pre-submission checks on a .sub file and print a report of every check:\npassed (green), failed (red), warnings (amber), needs AI (purple), and could not run\nor not implemented (grey; a check that could not run says why). --colorblind\nswitches to a colour-blind-friendly palette.",
        epilog=_VALIDATE_CHECKS,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_validate.add_argument("subfile", help="path to the .sub file to check, e.g. biorxiv.sub.txt")
    p_validate.add_argument("--web", action="store_true", help="show the checks in the paperpush web page on this computer instead of the terminal, where you can fix the fields and re-validate; edits are saved to the .sub (with --ai, the AI checks run too). Needs pip install 'paperpush[web]'")
    p_validate.add_argument(
        "--dont-check-links",
        dest="check_links",
        action="store_false",
        help="skip checking that the URLs cited in the manuscript files are " "reachable. By default validate probes them and warns about broken " "links (404/gone, including still-private GitHub repos), which requires " "network access.",
    )
    p_validate.add_argument(
        "--dont-check-for-sensitive-info",
        dest="check_sensitive",
        action="store_false",
        help="skip scanning the referenced files for information not meant to " "be published. By default validate scans them for API keys, passwords, " "private keys, GPS coordinates in figures, editable-document links, and " "LaTeX source comments, nudges when no public code repository is linked, " "and reminds arXiv submitters to run arxiv_latex_cleaner on unclean " "source. Reported as advisory warnings.",
    )
    p_validate.add_argument(
        "--dont-check-references",
        dest="check_references",
        action="store_false",
        help="skip checking the references against the DOI registry. By " "default validate reads the submission's bibliography -- its .bib " "files, and the reference list in the manuscript itself -- resolves " "each DOI through doi.org, and warns when one is " "malformed, duplicated, unregistered, or registered to a different " "title, author, or year than the reference claims -- the usual sign of " "a DOI copied from the wrong reference. Requires network access.",
    )
    p_validate.add_argument(
        "--dont-check-manuscript",
        dest="check_manuscript",
        action="store_false",
        help="skip measuring the uploads against the venue's author guidelines. " "By default validate reads manuscript_requirements.json for the venue " "and checks the manuscript's format, word/page count (compiling LaTeX " "source to a scratch PDF with latexmk/pdflatex when installed), " "required section headings and declarations, title-page items, " "abstract/title/keyword limits, figure format, resolution, and count, " "supplementary-file rules, and the number of references. See " "'paperpush requirements VENUE' for the rules applied.",
    )
    p_validate.add_argument(
        "--anonymous",
        dest="check_anonymous",
        action="store_const",
        const=True,
        help="check the submission for information identifying the authors, as " "for a double-blind venue. Venues marked anonymous (e.g. ICLR, AAAI) " "get this check automatically. Scans the attached files and the .sub's " "text fields for the authors' names, emails, ORCID/OpenReview IDs and " "affiliations, author metadata (PDF, Office, EXIF), home-directory " "paths, acknowledgments, camera-ready LaTeX switches, and non-anonymized " "github.com links, and fetches " "every linked anonymous.4open.science repository to scan it the same " "way (requires network access).",
    )
    p_validate.add_argument(
        "--dont-check-hidden-text",
        dest="check_hidden_text",
        action="store_false",
        help="skip looking for text a reader cannot see. By default validate " "reads every PDF, LaTeX, and Word upload for white, invisible, " "microscopic, or off-page text; hidden instructions to AI reviewers " "(prompt injection, grounds for desk rejection) are errors, other " "hidden prose and visible text addressed to an AI reviewer are warnings.",
    )
    p_validate.add_argument(
        "--dont-check-openreview-profiles",
        dest="check_openreview",
        action="store_false",
        help="skip looking up the authors' OpenReview profiles. For venues " "submitted through OpenReview (ICLR, AAAI), validate signs in with the " "login stored by 'paperpush login' and checks that every author has an " "active profile matching their name and email suffixes, with a current " "position. Requires network access.",
    )
    p_validate.add_argument(
        "--arxiv-latex-cleaner",
        dest="arxiv_latex_cleaner",
        action="store_true",
        help="run arxiv_latex_cleaner on the LaTeX source first (every .tex upload, " "and every .zip/.tar/.tar.gz/.tgz bundle containing one), then validate " "the cleaned copies. Strips comments and unreferenced or auxiliary files. " "Cleaned copies are written next to the originals (paper/ -> paper_arXiv/, " "source.zip -> source_arXiv.zip); the originals and the .sub are not " "changed. Needs the optional package: pip install 'paperpush[validate]'.",
    )
    p_validate.add_argument(
        "-o",
        "--output",
        nargs="?",
        const="",
        metavar="OUT",
        help="also save the report. PDF by default (keeps the colours); give a path ending in .html, .md, or .txt for those formats. With no OUT, writes <subfile>_validation.pdf next to the .sub.",
    )
    p_validate.add_argument("--overwrite", action="store_true", help="replace the report saved with -o if the file already exists")
    p_validate.add_argument("--force", dest="overwrite", action="store_true", help=argparse.SUPPRESS)
    p_validate.add_argument(
        "--initial-submission",
        dest="initial_submission",
        action="store_true",
        help="validate this as an initial submission: every error is reported as a warning, so nothing blocks it. Meant for venues whose guidelines let the initial submission be in any format (the report names the rule at the top when the venue has one; see 'paperpush requirements VENUE').",
    )
    p_validate.add_argument(
        "--cover-letter-max-age",
        dest="cover_letter_max_age",
        type=int,
        default=7,
        metavar="DAYS",
        help="warn when the date at the top of the cover letter is more than DAYS days old (default: 7). The cover letter is also checked for the name of a different venue, as left over from an earlier submission.",
    )
    p_validate.add_argument("--colorblind", "--alt-color", dest="alt_color", action="store_true", help="use colour-blind-friendly colours in the terminal and saved report: blue for passed, vermillion for failed, dark yellow for warnings, reddish purple for needs AI, grey for could not run / not implemented (the same palette as the web app's \"Colorblind-friendly colors\" setting). --alt-color is an older name for it")
    p_validate.add_argument(
        "--ai",
        action="store_true",
        help="also run the checks only an LLM can judge (lettering in raster figures, the guidelines' free-text rules, whether the .sub's fields say the same as the manuscript, ...) through an LLM API. Needs ANTHROPIC_API_KEY, OPENAI_API_KEY, or GEMINI_API_KEY and the provider's SDK (pip install 'paperpush[ai]'). Without it those checks are listed as needing AI.",
    )
    p_validate.add_argument("--provider", choices=["anthropic", "openai", "google"], help="(--ai) LLM provider; defaults to the first whose API key is set")
    p_validate.add_argument("--model", help="(--ai) model to use (default: the provider's default, as for autofill)")
    p_validate.add_argument(
        "--ai-only",
        dest="ai_only",
        action="store_true",
        help="report only the checks that need AI, skipping the deterministic checks (form, links, DOIs, sensitive data, hidden text, ...). Combine with --ai, --ai-tasks, or --ai-results; on its own it lists the AI checks that apply. The manuscript is still read to find which AI checks apply.",
    )
    p_validate.add_argument("--ai-tasks", dest="ai_tasks", metavar="FILE", help="write the AI checks to FILE as JSON, for an agent (Claude Code, Codex, ...) to run with its own subscription; see the paperpush-validate skill")
    p_validate.add_argument("--ai-results", dest="ai_results", metavar="FILE", help='merge an agent\'s verdicts on the AI checks (JSON: {"results": [{"id", "status": pass|fail|unclear|not_applicable, "details"}]}) into the report instead of calling an API')
    p_validate.set_defaults(func=_cmd_validate, arxiv_latex_cleaner=False, check_links=True, check_sensitive=True, check_references=True, check_manuscript=True, check_anonymous=None, check_hidden_text=True, check_openreview=True, initial_submission=False)

    p_requirements = sub.add_parser("requirements", parents=[verbosity], help="show the manuscript requirements recorded for a venue's author guidelines")
    p_requirements.add_argument("venue", help="venue slug, e.g. nature")
    p_requirements.add_argument("--article-type", dest="article_type", metavar="TYPE", help="show the rules for this article type (the venue's article-type option string) instead of the default research article")
    p_requirements.add_argument("--json", action="store_true", help="print the requirements as JSON instead of a readable summary")
    p_requirements.set_defaults(func=_cmd_requirements)

    p_login = sub.add_parser("login", parents=[verbosity], help="store credentials for a venue submission system")
    p_login.add_argument("venue", nargs="?", help="venue slug, e.g. biorxiv (omit with --list)")
    p_login.add_argument("--list", action="store_true", help="list the venues you are logged in to with usernames, then exit")
    p_login.add_argument("-u", "--username", help="username or email (otherwise you are prompted or it looks " "for the PAPERPUSH_USERNAME environment variable)")
    p_login.add_argument(
        "--password",
        help="password (otherwise you are prompted or it looks for the " "PAPERPUSH_PASSWORD environment variable). WARNING: exposes " "the password as plain text in the process list and shell history",
    )
    p_login.add_argument(
        "--confirm-password",
        dest="confirm_password",
        action="store_true",
        help="ask for the password twice and check the two match " "(by default it is asked for once)",
    )
    p_login.add_argument(
        "--orcid",
        action="store_true",
        help="sign in with your ORCID account instead of a venue " "username/password: prompts for your ORCID iD and ORCID " "password, and signs in through the venue's 'Sign in with " "ORCID' button. Journals only",
    )
    p_login.add_argument("--orcid-id", metavar="ID", dest="orcid_id", help="your ORCID iD (e.g. 0000-0002-1825-0097) or the email " "registered with ORCID; implies --orcid and skips that " "prompt (you are still asked for the ORCID password)")
    p_login.add_argument("--into", metavar="SUBFILE", help="after an ORCID login, fill the matching author's " "ORCID/name/affiliation in this .sub file, read from " "their public ORCID record")
    p_login.add_argument("--status", action="store_true", help="show whether credentials are stored, then exit")
    p_login.add_argument(
        "--check",
        action="store_true",
        help="sign in with the stored credentials (headless) to confirm " "they still work, then exit; unlike --status this catches a " "changed password. Exits 1 if nothing is stored or the sign-in fails",
    )
    p_login.add_argument("--logout", action="store_true", help="remove stored credentials for the venue")
    p_login.add_argument("--no-verify", dest="no_verify", action="store_true", help="store the credentials without checking them against " "the venue's sign-in (the default opens a browser and " "verifies first)")
    p_login.add_argument("--verify-headless", dest="verify_headless", action="store_true", help="run the verification browser headless (no window; " "cannot complete a CAPTCHA or two-factor prompt)")
    p_login.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        metavar="SECONDS",
        help="cap how long the verification browser waits for any " "action or page load before failing (default: " f"{DEFAULT_TIMEOUT_SECONDS:g}s; 0 waits forever)",
    )
    p_login.set_defaults(func=_cmd_login)

    p_submit = sub.add_parser("submit", parents=[verbosity], help="open the venue submission portal and run the submission click-through")
    p_submit.add_argument("subfile", help="path to the .sub file")
    p_submit.add_argument("--headless", action="store_true", help="run the browser headless (default: headed, so you " "can sign in and review)")
    p_submit.add_argument("--debug", action="store_true", help="open the Playwright Inspector at the first step " "to walk the wizard line by line; reuses a saved " "session from an earlier run to skip sign-in")
    p_submit.add_argument("--new-session", action="store_true", help="discard any saved browser session and sign in " "fresh (use after switching accounts)")
    p_submit.add_argument("--skip-validate", action="store_true", help="do not run validate on the .sub before opening the browser " "(default: validate first and stop on any error)")
    p_submit.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        metavar="SECONDS",
        help="cap how long the browser waits for any action or page " f"load before failing (default: {DEFAULT_TIMEOUT_SECONDS:g}s; " "0 waits forever)",
    )
    p_submit.add_argument(
        "--close-on-failure",
        action="store_true",
        help="close the browser when the run fails (default: leave the " "window open at the step that broke so you can see the page " "and finish by hand); --headless always closes",
    )
    p_submit.add_argument(
        "--recover",
        choices=["off", "by-hand", "ai", "agent"],
        default=None,
        help="what to do when one step of the click-through fails. off (default): stop the run. "
        "by-hand: pause and ask here; fix the page in the browser, then continue or retry the step. "
        "ai: have an LLM fix the page (same as --ai). "
        "agent: pause for an AI agent to fix the page through --recover-dir (the MCP server uses this). "
        "Sign-in is never part of recovery; a venue whose runner does not support it ignores the option",
    )
    p_submit.add_argument(
        "--ai",
        action="store_true",
        help="when a step fails, send the page (its accessibility tree and a screenshot) to an LLM, which fixes it "
        "in the browser so the run can carry on; it is never allowed to send the submission. Needs an API key, "
        "as for 'autofill --engine api'. In a terminal, a step the model gives up on is handed to you",
    )
    p_submit.add_argument("--provider", choices=["anthropic", "openai", "google"], help="(--ai) LLM provider; defaults to the first of anthropic, openai, google " "whose API key is set (ANTHROPIC_API_KEY, OPENAI_API_KEY, GEMINI_API_KEY/GOOGLE_API_KEY)")
    p_submit.add_argument("--model", help="(--ai) model to use (default: the provider's default, as for autofill)")
    p_submit.add_argument("--recover-dir", metavar="DIR", help="(--recover agent, by-hand, or --ai) directory for the recovery log, screenshots, and (agent) the " "files the paused run and the agent exchange (default: a fresh one under paperpush's config dir)")
    p_submit.add_argument(
        "--cdp-port",
        type=int,
        metavar="PORT",
        help="open a Chrome DevTools Protocol endpoint on 127.0.0.1:PORT (0 picks a free port), so another "
        "client such as 'playwright-cli attach --cdp=<url>' can drive the same browser. Anything on this "
        "machine can then control the signed-in browser, and the agent guardrails (no final submit, "
        "uploads limited to .sub files) do not apply to it",
    )
    p_submit.set_defaults(func=_cmd_submit)

    p_codegen = sub.add_parser("codegen", parents=[verbosity], help="record a venue's submission wizard as runner code")
    p_codegen.add_argument("url", nargs="?", help="page to start on (default: the venue's portal, else a blank page)")
    p_codegen.add_argument("--venue", help="venue slug: its fields become the variables and its saved sign-in is reused")
    p_codegen.add_argument("--sub", metavar="SUBFILE", help="a filled .sub: typed values and files matching it are bound to its fields, and Play replays its values")
    p_codegen.add_argument("--target", choices=["paperpush", "python"], default="paperpush", help="paperpush: the body of a venue runner's submit() (default); python: a standalone Playwright script")
    p_codegen.add_argument("-o", "--output", metavar="OUT", help="write the code here, updated as you record (default: print it at the end)")
    p_codegen.add_argument("--new-session", dest="new_session", action="store_true", help="start signed out instead of reusing the venue's saved sign-in")
    p_codegen.set_defaults(func=_cmd_codegen)

    p_web = sub.add_parser(
        "web",
        parents=[verbosity],
        help="open the paperpush web page on this computer; with a .sub file, open it filled in (edits are saved to the file)",
        description="Serve the paperpush web page on this computer and open it in your browser. "
        "Given a .sub file, the page opens with it filled in, and every edit there is saved back to the file. "
        "Needs pip install 'paperpush[web]'.",
    )
    p_web.add_argument("subfile", nargs="?", help="a .sub file to open, e.g. biorxiv.sub.txt")
    p_web.add_argument("-p", "--path", dest="paths", action="append", metavar="PATH", help="a manuscript directory (or a file in it) whose files the page offers in its file pickers (with a .sub file; may be repeated)")
    p_web.add_argument("-d", "--directory", dest="paths", action="append", help=argparse.SUPPRESS)  # old name for -p/--path
    p_web.add_argument("--port", type=int, default=0, help="port to listen on (default: any free port)")
    p_web.add_argument("--no-browser", action="store_true", help="print the address instead of opening a browser")
    # Used by the background preview 'subfile' and 'autofill' start: stop after this many idle seconds.
    p_web.add_argument("--idle-timeout", type=float, default=None, help=argparse.SUPPRESS)
    p_web.set_defaults(func=_cmd_web)

    p_pick = sub.add_parser(
        "pick-venue",
        parents=[verbosity],
        help="find venues for a manuscript, from its keywords, title, and abstract",
        description="Rank the supported venues for a manuscript.\n\n"
        "  paperpush pick-venue hippocampus 'place cells'      keywords (a venue subject such as 'immunology' matches directly)\n"
        "  paperpush pick-venue paper.pdf                      a manuscript (.tex, .zip, .docx, .pdf, or a .txt/.md with its title and abstract)\n"
        "  paperpush pick-venue --title T --abstract A memory  the parts separately\n"
        "  paperpush pick-venue --venue-name 'nature neuro'    venues by name\n"
        "  paperpush pick-venue paper.pdf --ai                 an LLM's ranking, with a reason for each venue\n\n"
        "Keywords and the title count three times as much as the abstract when they are matched to venue subjects "
        "with a built-in word list (offline); --ai asks an LLM instead and needs a provider API key. "
        "Only venues paperpush supports are ranked, on thematic fit alone.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_pick.add_argument("keywords_pos", nargs="*", metavar="KEYWORD", help="the manuscript's keywords, comma-separated as in a paper's keyword line (words without commas form one phrase, e.g. single cell RNA-seq, T cell exhaustion). A single existing file is read as --manuscript instead")
    p_pick.add_argument("--manuscript", metavar="PATH", help="a manuscript (.tex, .zip, .docx, .pdf) to read the title, abstract, and keywords from, or a .txt/.md holding them")
    p_pick.add_argument("--title", metavar="TEXT", help="the manuscript's title (replaces the one read from --manuscript)")
    p_pick.add_argument("--abstract", metavar="TEXT", help="the manuscript's abstract (replaces the one read from --manuscript)")
    p_pick.add_argument("-k", "--keywords", action="append", metavar="KW[,KW...]", help="more keywords, as for KEYWORD (repeatable; added to those read from --manuscript)")
    p_pick.add_argument("--venue-name", "--journal-name", "--conference-name", dest="venue_name", metavar="NAME", help="search venues by slug, name, or full name, e.g. 'nature methods' or 'neuro'")
    p_pick.add_argument("-t", "--type", dest="types", action="append", choices=["preprint", "journal", "conference"], metavar="TYPE", help="only venues of this kind (repeatable)")
    p_pick.add_argument("--portal", dest="portal", action="append", metavar="PORTAL", help="only venues submitting through this portal (repeatable), e.g. scholarone")
    p_pick.add_argument("-n", "--limit", type=int, default=5, metavar="N", help="show at most N matches (default: 5)")
    p_pick.add_argument("--all", action="store_true", help="show every match instead of limiting")
    p_pick.add_argument("--ai", action="store_true", help="ask an LLM to choose the subjects and rank the venues, with a reason each (needs ANTHROPIC_API_KEY, OPENAI_API_KEY, or GEMINI_API_KEY)")
    p_pick.add_argument("--provider", choices=["anthropic", "openai", "google"], help="(--ai) LLM provider; defaults to the first whose API key is set")
    p_pick.add_argument("--model", help="(--ai) model to use (default: the provider's autofill default)")
    p_pick.add_argument("--json", action="store_true", help="print the result as JSON for agents: matches (slug, name, venue_type, portal, keywords, score, reasons), the subjects used, what was read from the manuscript, and the --ai ranking")
    p_pick.set_defaults(func=_cmd_pick_venue)

    p_add = sub.add_parser(
        "add-venue",
        parents=[verbosity],
        help="add a venue from a venue file (--template writes one to fill in)",
        description="Add a venue to paperpush, or fix one, from a plain-text venue file.\n\n"
        "  paperpush add-venue --template my_journal     write my_journal.txt to fill in\n"
        "  paperpush add-venue --template --from nature  write nature.txt from Nature's current entries\n"
        "  paperpush add-venue my_journal.txt            check it and merge it into the venue databases\n\n"
        "The file has a section for the submission portal's form (submission_forms.json) and one for the\n"
        "author guidelines (manuscript_requirements.json); either may be left out. The web page's\n"
        "'Add venue' button fills the same file in through a form.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_add.add_argument("file", nargs="?", metavar="FILE", help="the venue file to add; with --template, the new venue's slug (optional)")
    p_add.add_argument("--template", action="store_true", help="write a venue file to fill in instead of adding one")
    p_add.add_argument("--from", dest="from_slug", metavar="SLUG", help="(--template) start from an existing venue's entries, e.g. to fix them or to add a sister journal")
    p_add.add_argument("-o", "--output", metavar="OUT", help="(--template) where to write the file (default: SLUG.txt; '-' prints it)")
    p_add.add_argument("--overwrite", action="store_true", help="overwrite an existing file (--template), or replace an existing venue's entries")
    p_add.add_argument("--force", dest="overwrite", action="store_true", help=argparse.SUPPRESS)  # old name for --overwrite
    p_add.add_argument("--dry-run", action="store_true", help="check the file and print the entries it would add, without writing them")
    p_add.add_argument("--data-dir", dest="data_dir", metavar="DIR", help="write the databases in DIR instead of the installed package (copied there first if missing); use them with PAPERPUSH_VENUE_DATA=DIR")
    p_add.set_defaults(func=_cmd_add_venue)

    p_update = sub.add_parser("update-venues", parents=[verbosity], help="fetch the latest published venue data (otherwise refreshed daily)")
    p_update.add_argument("--clear", action="store_true", help="delete the cached copy and use the data bundled with this version")
    p_update.set_defaults(func=_cmd_update_venues)

    p_skills = sub.add_parser(
        "install-skills",
        parents=[verbosity],
        help="install paperpush's agent skills (autofill, validate, submit, portal debugging) for Claude Code or Codex",
        description="Install the agent skills that ship with paperpush, so an AI agent knows how to autofill, validate, and submit with it.\n\n"
        "  paperpush install-skills                  the author skills, for Claude Code in every project\n"
        "  paperpush install-skills --all           also the contributor skills (adding and fixing venues)\n"
        "  paperpush install-skills --agent codex   for Codex instead\n"
        "  paperpush install-skills --project .     into this project only\n"
        "  paperpush install-skills --list          show the skills and whether each is installed\n"
        "  paperpush install-skills --uninstall     remove them again\n\n"
        "Run it again after upgrading paperpush to update the installed copies.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_skills.add_argument("names", nargs="*", metavar="SKILL", help="install only these skills, e.g. paperpush-autofill (or just 'autofill'); default: every author skill")
    p_skills.add_argument("--agent", choices=sorted(("claude", "codex")), default="claude", help="the agent to install for (default: claude, into ~/.claude/skills; codex: ~/.codex/skills)")
    where = p_skills.add_mutually_exclusive_group()
    where.add_argument("--project", nargs="?", const=".", metavar="DIR", help="install into the project at DIR (default: the current directory) instead of your user skills directory")
    where.add_argument("--dest", metavar="DIR", help="install into this skills directory instead, for another agent")
    p_skills.add_argument("--all", action="store_true", help="also install the contributor skills (paperpush-add-venue, paperpush-add-venue-validate), which need a source checkout")
    p_skills.add_argument("--overwrite", action="store_true", help="replace a skill directory of the same name that this command did not install")
    action = p_skills.add_mutually_exclusive_group()
    action.add_argument("--list", action="store_true", help="list the skills and whether each is installed, then exit")
    action.add_argument("--uninstall", action="store_true", help="remove the skills this command installed")
    p_skills.set_defaults(func=_cmd_install_skills)

    p_report = sub.add_parser("recovery-report", parents=[verbosity], help="draft a redacted bug report from a submit run whose steps needed recovery")
    p_report.add_argument("directory", nargs="?", help="the run's recovery directory (default: the latest run with recoveries)")
    p_report.add_argument("--subfile", metavar="SUB", help="the run's .sub, to mask its values (default: the one the run recorded)")
    p_report.add_argument("--json", action="store_true", help="print the draft as JSON")
    p_report.add_argument("--open", action="store_true", help="open the prefilled GitHub issue page in your browser (you still review and submit it)")
    p_report.set_defaults(func=_cmd_recovery_report)

    # Internal command: the autofill front-ends (the Claude skill and the API
    # engine) read field roles from here. Hidden from --help (use 'subfile' to
    # inspect a venue's fields by hand).
    p_schema = sub.add_parser("schema", parents=[verbosity])
    p_schema.add_argument("venue", help="venue slug, e.g. biorxiv")
    p_schema.set_defaults(func=_cmd_schema)

    return parser


@_validate
def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    configure_logging(verbosity=getattr(args, "verbose", 0), quiet=getattr(args, "quiet", False))
    logger.debug("dispatch: command=%r", getattr(args, "command", None))

    if args.agent_guide:
        return _print_agent_guide()

    if not getattr(args, "command", None):
        parser.print_help()
        return 0

    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
