#!/usr/bin/env python3
"""Run the full paperpush submission pipeline with a single command.

This drives the public ``paperpush`` subcommands back to back, in the
order they are meant to be used:

    1. subfile   create <venue>.sub from the venue's template
    2. autofill  populate that .sub from a manuscript directory
    3. validate  run the pre-submission checks on the filled .sub
    4. login     store credentials for the venue's submission portal
    5. submit    open the portal and run the submission click-through

Validate runs right after autofill so any required field, missing file, or bad
value is caught before credentials are stored and the portal opens. Login runs
just before submit so credentials are stored right when the portal is about to
open.

The arguments below are the union of the subcommands' arguments. Anything
specific to one step (e.g. ``--values`` for autofill, ``--headless`` for submit)
is forwarded only to that step; the shared values -- the venue slug, the .sub
path, ``--force`` -- are threaded through every step that needs them. The .sub
path is derived from the venue slug (or ``--output``) once and reused, so the
file created by ``subfile`` is exactly the one ``autofill`` fills and ``submit``
reads.

Steps run sequentially and the pipeline stops at the first failure, returning
that step's exit code.

Examples:

    # Manual autofill (the values come from a JSON file, e.g. one a tool wrote):
    python scripts/paperpush_pipeline.py biorxiv \\
        -d ./my_manuscript --values values.json

    # API autofill, then sign in with ORCID (journals that offer it), run headless:
    python scripts/paperpush_pipeline.py cell_genomics \\
        -d ./my_manuscript --engine api --orcid --headless

    # Re-run end to end, overwriting an existing nature.sub:
    python scripts/paperpush_pipeline.py nature -d ./ms --values v.json --force
"""

from __future__ import annotations

import argparse
import sys
from argparse import Namespace
from pathlib import Path

# Import the package directly from the repo without requiring an install.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from paperpush._logging import configure_logging  # noqa: E402
from paperpush.cli import (_cmd_autofill, _cmd_login,  # noqa: E402
                           _cmd_subfile, _cmd_submit, _cmd_validate)
from paperpush.database import get_venue  # noqa: E402
from paperpush.subfile import default_filename  # noqa: E402
from paperpush.venues.common import DEFAULT_TIMEOUT_SECONDS  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    """Parser whose options are the union of the four subcommands' options."""
    parser = argparse.ArgumentParser(
        prog="paperpush_pipeline",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # Shared across steps.
    parser.add_argument("venue", help="venue slug, e.g. biorxiv (used by subfile and login)")
    parser.add_argument("-o", "--output", help="path for the .sub file (default: <venue>.sub); the same file is filled and submitted")
    parser.add_argument("--force", action="store_true", help="overwrite an existing .sub file (applies to subfile and autofill)")
    parser.add_argument("-v", "--verbose", action="count", default=0, help="increase logging verbosity (-v info, -vv debug)")
    parser.add_argument("-q", "--quiet", action="store_true", help="only log errors")

    # subfile-only.
    subfile = parser.add_argument_group("subfile step")
    subfile.add_argument("--dont-fill-defaults", dest="fill_defaults", action="store_false", help="leave fields with default values empty instead of pre-populating them")
    subfile.set_defaults(fill_defaults=True)

    # autofill-only.
    autofill = parser.add_argument_group("autofill step")
    autofill.add_argument("-d", "--directory", required=True, metavar="MANUSCRIPTDIR", help="directory holding the manuscript, figures, and other files")
    autofill.add_argument("--engine", choices=["manual", "api"], default="manual", help="manual: read proposed values from --values (default); api: extract them with the Anthropic API")
    autofill.add_argument("--values", metavar="FILE", help="JSON file of proposed field values (required for --engine manual)")
    autofill.add_argument("--manuscript", metavar="FILE", help="(api) the manuscript file; inferred from the directory if omitted")
    autofill.add_argument("--title-page", dest="title_page", metavar="FILE", help="(api) a standalone title page with author details, if separate")
    autofill.add_argument("--supplement", metavar="FILE", help="(api) a supplementary materials file, if any")
    autofill.add_argument("--model", default="claude-opus-4-8", help="(api) Anthropic model to use (default: claude-opus-4-8)")
    autofill.add_argument("--min-confidence", choices=["low", "medium", "high"], default="low", help="do not write any value below this confidence (default: low)")
    autofill.add_argument("--dry-run", action="store_true", help="show what autofill would write without changing the .sub file")

    # login-only.
    login = parser.add_argument_group("login step")
    login.add_argument("-u", "--username", help="username or email (otherwise you are prompted)")
    login.add_argument("--orcid", action="store_true", help="sign in with your ORCID account (prompts for your ORCID iD and ORCID password) instead of a venue username/password; journals only")
    login.add_argument("--orcid-id", metavar="ID", dest="orcid_id", help="your ORCID iD (e.g. 0000-0002-1825-0097) or registered email; implies --orcid")
    login.add_argument("--into", metavar="SUBFILE", help="after an ORCID login, fill the matching author's ORCID/name/affiliation in this .sub file")
    login.add_argument("--status", action="store_true", help="show whether credentials are stored instead of signing in")
    login.add_argument("--logout", action="store_true", help="remove stored credentials for the venue")

    # submit-only.
    submit = parser.add_argument_group("submit step")
    submit.add_argument("--headless", action="store_true", help="run the browser headless (default: headed, so you can sign in and review)")
    submit.add_argument("--debug", action="store_true", help="open the Playwright Inspector at the first step to walk the wizard line by line")
    submit.add_argument("--new-session", action="store_true", help="discard any saved browser session and sign in fresh")

    return parser


def _resolve_sub_path(args: argparse.Namespace, venue) -> Path:
    """The single .sub path used across all steps (mirrors ``subfile``'s logic)."""
    if args.output:
        target = Path(args.output)
        if target.suffix != ".sub":
            target = target.with_name(target.name + ".sub")
        return target
    return Path(default_filename(venue))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(verbosity=args.verbose, quiet=args.quiet)

    # Validate the venue once up front and derive the shared .sub path so every
    # step agrees on which file is being created, filled, and submitted.
    try:
        venue = get_venue(args.venue)
    except KeyError:
        print(f"error: unknown venue '{args.venue}'", file=sys.stderr)
        print("Run 'paperpush --venues' to see supported venues.", file=sys.stderr)
        return 2
    sub_path = str(_resolve_sub_path(args, venue))

    steps = [
        (
            "subfile",
            _cmd_subfile,
            Namespace(
                venue=args.venue,
                output=sub_path,
                fill_defaults=args.fill_defaults,
                force=args.force,
            ),
        ),
        (
            "autofill",
            _cmd_autofill,
            Namespace(
                subfile=sub_path,
                directory=args.directory,
                engine=args.engine,
                values=args.values,
                manuscript=args.manuscript,
                title_page=args.title_page,
                supplement=args.supplement,
                model=args.model,
                min_confidence=args.min_confidence,
                dry_run=args.dry_run,
                output=None,  # fill the .sub created above in place
                force=args.force,
            ),
        ),
        (
            "validate",
            _cmd_validate,
            Namespace(subfile=sub_path),
        ),
        (
            "login",
            _cmd_login,
            # The full set of login arguments: _cmd_login reads them directly, so
            # every flag the CLI defines needs a value here even when the pipeline
            # exposes no switch for it. Passwords are never taken as a pipeline
            # flag -- login prompts, or reads PAPERPUSH_PASSWORD.
            Namespace(
                venue=args.venue,
                username=args.username,
                password=None,
                orcid=args.orcid,
                orcid_id=args.orcid_id,
                into=args.into,
                status=args.status,
                logout=args.logout,
                list=False,
                no_verify=False,
                verify_headless=args.headless,
                timeout=DEFAULT_TIMEOUT_SECONDS,
            ),
        ),
        (
            "submit",
            _cmd_submit,
            Namespace(
                subfile=sub_path,
                headless=args.headless,
                debug=args.debug,
                new_session=args.new_session,
            ),
        ),
    ]

    total = len(steps)
    for i, (name, func, ns) in enumerate(steps, start=1):
        print(f"\n=== Step {i}/{total}: {name} ===\n")
        rc = func(ns)
        if rc != 0:
            print(f"\nPipeline stopped: '{name}' exited with code {rc}.", file=sys.stderr)
            return rc

    print("\nPipeline complete. Review the submission in the portal before clicking submit.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
