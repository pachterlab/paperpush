"""Command-line interface for paperpush.

Implemented so far:

    paperpush --venues          list supported venues
    paperpush subfile <venue>   create a <venue>.sub template
    paperpush options <venue>.<field>
                                      list the allowed values for a field
    paperpush autofill <subfile> -d <dir>
                                      fill a .sub from a manuscript directory
    paperpush validate <subfile>  run the pre-submission checks on a .sub
                                      (scans referenced files for secrets, GPS
                                      metadata, LaTeX comments, and broken links
                                      by default; --dont-check-* to opt out)
    paperpush login <venue>     store credentials for a venue
    paperpush login --list        list the venues you are logged in to
    paperpush login --orcid <j>   store an ORCID iD/password for a journal that
                                      offers "Sign in with ORCID"
    paperpush submit <subfile>    open bioRxiv and run the submission
    paperpush agent-guide         print the guide for AI agents (AGENTS.md)

``submit`` drives the bioRxiv wizard, typing in the field values read from the
.sub file and stopping before the final submit. The first run signs in (reusing
stored credentials where possible) and saves the browser session, so later runs
start already authenticated.
"""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import sys
from pathlib import Path

from pydantic import ConfigDict, validate_call

from . import __url__, __version__, credentials
from ._logging import configure_logging
from .database import get_venue, list_venues
from .subfile import default_filename, write_template
from .venues.common import DEFAULT_TIMEOUT_SECONDS

logger = logging.getLogger(__name__)

# argparse.Namespace is not a pydantic type, so allow it through validation.
_validate = validate_call(config=ConfigDict(arbitrary_types_allowed=True))


# Ordered venue_type groups for `--venues`, mirroring the README's "Supported
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
def _cmd_agent_guide(args: argparse.Namespace) -> int:
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


@_validate
def _print_venues() -> int:
    venues = list_venues()
    if not venues:
        print("No venues are configured.")
        return 0
    by_type: dict[str, list] = {}
    for venue in venues:
        by_type.setdefault(venue.venue_type, []).append(venue)

    for i, (venue_type, heading) in enumerate(_VENUE_GROUPS):
        if i:
            print()
        print(f"{heading}:")
        members = by_type.get(venue_type, [])
        if not members:
            print("  (none yet)")
            continue
        for venue in members:
            print(f"  {venue.name} ({venue.slug})")

    print(f"\nFor more details, see {_VENUES_URL}")
    return 0


@_validate
def _cmd_subfile(args: argparse.Namespace) -> int:
    logger.debug("subfile: requested venue %r, output=%r, fill_defaults=%s, " "force=%s", args.venue, args.output, args.fill_defaults, args.force)
    try:
        venue = get_venue(args.venue)
    except KeyError:
        logger.warning("subfile: unknown venue %r", args.venue)
        print(f"error: unknown venue '{args.venue}'", file=sys.stderr)
        print("Run 'paperpush --venues' to see supported venues.", file=sys.stderr)
        return 2

    if args.output:
        target = Path(args.output)
        if target.suffix != ".sub":
            target = target.with_name(target.name + ".sub")
    else:
        target = Path(default_filename(venue))
    try:
        path = write_template(
            venue,
            path=target,
            fill_defaults=args.fill_defaults,
            overwrite=args.force,
        )
    except FileExistsError:
        logger.info("subfile: refusing to overwrite existing %s", target)
        print(f"error: {target} already exists (use --force to overwrite)", file=sys.stderr)
        return 1

    required = sum(1 for f in venue.fields if f.required)
    logger.info("subfile: wrote template %s for %s (%d fields, %d required)", path, venue.slug, len(venue.fields), required)
    print(f"Created {path} for {venue.slug}.")
    print(f"  {len(venue.fields)} fields ({required} required). " "Open it in your editor and fill in the values.")
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


@_validate
def _cmd_login(args: argparse.Namespace) -> int:
    if getattr(args, "list", False):
        return _list_logins()

    if not args.venue:
        print("error: a venue is required (or use --list)", file=sys.stderr)
        print("Run 'paperpush --venues' to see supported venues.", file=sys.stderr)
        return 2

    logger.debug("login: venue=%r orcid=%s status=%s logout=%s", args.venue, bool(args.orcid or args.orcid_id), args.status, args.logout)
    try:
        venue = get_venue(args.venue)
    except KeyError:
        logger.warning("login: unknown venue %r", args.venue)
        print(f"error: unknown venue '{args.venue}'", file=sys.stderr)
        print("Run 'paperpush --venues' to see supported venues.", file=sys.stderr)
        return 2

    # A venue that submits through another's portal (e.g. the AAAS family)
    # shares that base venue's account, so its credentials live under the base
    # slug. Redirect to the base so storage, status, and logout all act on the one
    # shared login. (Schema inheritance is separate -- see submission_base.)
    from . import venues

    sub_base = venues.submission_base(venue.slug)
    if sub_base != venue.slug:
        print(f"{venue.slug} shares the {sub_base} sign-in; storing under '{sub_base}'.")
        venue = get_venue(sub_base)

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


def _report_validation(subfile, venue_def, subfile_path: str, *, check_sensitive: bool = True, check_links: bool = True) -> list:
    """Validate a loaded .sub against its venue and print the findings.

    Runs the same checks ``submit`` performs before opening a browser --
    required/missing fields, files that must exist, allowed options, file types,
    and booleans -- printing warnings (advisory) and an error summary (blocking)
    to stderr. Returns the list of error :class:`~paperpush.validate.Issue`
    objects (empty when the file passes), so the caller decides what to do next.
    Shared by ``submit`` (the gate before submission) and the standalone
    ``validate`` command.

    When ``check_links`` is set (the default), the URLs cited in the referenced
    files are probed and broken ones reported (makes network requests). When
    ``check_sensitive`` is set, those files are additionally scanned for
    information not meant to be published (secrets, GPS metadata,
    editable-document links, LaTeX comments); all surface as advisory warnings.
    """
    from .validate import validate

    issues = validate(subfile, venue_def, check_sensitive=check_sensitive, check_links=check_links)
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


@_validate
def _cmd_validate(args: argparse.Namespace) -> int:
    """Run the pre-submission checks on a .sub file.

    This surfaces, as a standalone command, exactly the validation ``submit``
    performs before it opens the portal: it loads the .sub file, looks up the
    venue it declares, and reports every required/missing field, missing file,
    bad option, and malformed value. Exits 0 when the file is ready to submit, 1
    when it has errors, and 2 when the venue is unknown.
    """
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
        print("Run 'paperpush --venues' to see supported venues.", file=sys.stderr)
        return 2

    errors = _report_validation(
        subfile,
        venue_def,
        args.subfile,
        check_sensitive=getattr(args, "check_sensitive", True),
        check_links=getattr(args, "check_links", True),
    )
    if errors:
        print("\nFix the items above, then run 'paperpush validate' again.", file=sys.stderr)
        return 1
    print(f"{args.subfile} passed validation for {venue}; ready to submit.")
    return 0


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
    if venue_def is not None:
        errors = _report_validation(subfile, venue_def, args.subfile)
        if errors:
            print("\nFix the items above, then run 'paperpush submit' again.", file=sys.stderr)
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
    if credentials.get_credential(cred_slug) is None:
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
        print(
            "The Playwright Inspector opens at the first step; use 'Step "
            "over' to walk the wizard line by line. If a saved session "
            "exists (from an earlier submit run) you start signed in; "
            "otherwise sign in in the browser, then resume."
        )
    else:
        print(f"Opening {venue} to run the submission for {args.subfile}…")
        print(
            "Sign-in is automatic when possible: a saved session is reused, "
            f"else your 'paperpush login {venue}' credentials are "
            "filled in (and the session saved); otherwise sign in by hand. "
            "Field values come from the .sub file; the wizard stops before "
            "the final submit."
        )
    logger.info("submit: launching %s runner (headless=%s, debug=%s, timeout=%ss, " "keep_open_on_failure=%s)", venue, args.headless, args.debug, args.timeout, not args.close_on_failure)
    run(subfile.values, headless=args.headless, debug=args.debug, new_session=args.new_session, timeout=args.timeout, keep_open_on_failure=not args.close_on_failure)
    logger.info("submit: %s runner returned", venue)
    return 0


@_validate
def _cmd_autofill(args: argparse.Namespace) -> int:
    """Populate a .sub file from values extracted from a manuscript directory.

    Loads (or, if absent, renders) the target .sub file, reads a set of proposed
    field values, and applies them through the shared autofill core: policy
    fields are left untouched, low-confidence and classified values are flagged
    for review, file paths are resolved against the manuscript directory, and
    the result is validated. The ``manual`` engine reads the proposals from a
    ``--values`` JSON file (this is the path the Claude skill drives); the
    ``api`` engine extracts them with the Anthropic API.
    """
    from .autofill import autofill, parse_extraction
    from .subfile import parse, render_template

    logger.debug(
        "autofill: subfile=%r dir=%r engine=%s values=%r " "min_confidence=%s dry_run=%s output=%r",
        args.subfile,
        args.directory,
        args.engine,
        args.values,
        args.min_confidence,
        args.dry_run,
        args.output,
    )

    manuscript_dir = Path(args.directory)
    if not manuscript_dir.is_dir():
        print(f"error: manuscript directory not found: {manuscript_dir}", file=sys.stderr)
        return 1

    if args.engine == "manual" and not args.values:
        print(
            "error: --values FILE is required with the manual engine.\n"
            "\n"
            "The manual engine applies field values that you extract yourself --\n"
            "it does not call any API. To produce them:\n"
            "  1. Run 'paperpush schema <venue>' to list the fields and roles.\n"
            "  2. Read the manuscript files and write a values.json (AGENTS.md has\n"
            "     the exact schema).\n"
            "  3. Re-run with --values values.json.\n"
            "Use '--engine api' instead only if ANTHROPIC_API_KEY is set and you\n"
            "want the Anthropic API to do the extraction.",
            file=sys.stderr,
        )
        return 1

    # Load the target .sub if it exists; otherwise render a fresh template,
    # taking the venue slug from the filename stem (e.g. biorxiv.sub).
    sub_path = Path(args.subfile)
    if sub_path.exists():
        try:
            text = sub_path.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"error: cannot read {sub_path}: {exc}", file=sys.stderr)
            return 1
        slug = parse(text).venue or sub_path.stem
    else:
        slug = sub_path.stem
        text = None

    try:
        venue = get_venue(slug)
    except KeyError:
        print(f"error: unknown venue '{slug}'", file=sys.stderr)
        print("Run 'paperpush --venues' to see supported venues.", file=sys.stderr)
        return 2
    if text is None:
        text = render_template(venue, fill_defaults=True)
        print(f"{sub_path} does not exist; starting from a fresh {venue.slug} " "template.\n")

    # Obtain the proposed field values: from the API, or from a --values file.
    if args.engine == "api":
        extraction = _extract_with_api(venue, manuscript_dir, args)
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

    result = autofill(text, venue, extraction, manuscript_dir=manuscript_dir, min_confidence=args.min_confidence)

    out_path = Path(args.output) if args.output else sub_path
    if args.dry_run:
        print(f"(dry run -- {out_path} not written)\n")
    else:
        if args.output and out_path.exists() and not args.force and out_path.resolve() != sub_path.resolve():
            print(f"error: {out_path} already exists (use --force to overwrite)", file=sys.stderr)
            return 1
        out_path.write_text(result.text, encoding="utf-8")

    # autofill is a preparation step, not the final gate: it deliberately leaves
    # policy fields (e.g. author consent) empty, so residual validation errors
    # are expected and reported as guidance rather than a nonzero exit. 'submit'
    # is what blocks on them.
    _print_autofill_summary(result, extraction, venue, out_path, args.dry_run)
    return 0


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


def _find_document(manuscript_dir: Path, explicit: str | None, stems: tuple[str, ...]) -> Path | None:
    """Resolve a named document in the manuscript directory.

    An explicit path is honored (resolved against the directory if relative);
    otherwise the top-level files are searched for one whose stem matches any of
    ``stems`` (case-insensitive). Returns None if nothing is found.
    """
    if explicit:
        p = Path(explicit)
        if not p.is_absolute() and not p.exists():
            p = manuscript_dir / explicit
        return p
    for child in sorted(manuscript_dir.iterdir()):
        if child.is_file() and child.stem.lower() in stems:
            return child
    return None


def _list_directory(manuscript_dir: Path) -> list[str]:
    """Relative paths of the files in the directory, for filemap assignment."""
    out: list[str] = []
    for path in sorted(manuscript_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(manuscript_dir)
        if any(part.startswith((".", "__")) for part in rel.parts):
            continue
        out.append(str(rel))
    return out


def _extract_with_api(venue, manuscript_dir: Path, args: argparse.Namespace):
    """Build the document set and run the API extraction engine.

    Returns an Extraction, or None after printing an error (so the caller can
    exit nonzero).
    """
    from .autofill import AutofillApiError, DocumentInput, extract_via_api

    manuscript = _find_document(manuscript_dir, args.manuscript, ("manuscript", "paper", "ms", "main"))
    if manuscript is None or not manuscript.is_file():
        print("error: could not find the manuscript; pass --manuscript <file>.", file=sys.stderr)
        return None

    documents = [DocumentInput("manuscript", manuscript)]
    title_page = _find_document(manuscript_dir, args.title_page, ("title_page", "titlepage", "title"))
    if title_page and title_page.is_file():
        documents.append(DocumentInput("title_page", title_page))
    supplement = _find_document(manuscript_dir, args.supplement, ("supplement", "supp", "si"))
    if supplement and supplement.is_file():
        documents.append(DocumentInput("supplement", supplement))

    labels = ", ".join(f"{d.label}={d.path.name}" for d in documents)
    print(f"Reading {labels} via the API ({args.model})…")
    try:
        return extract_via_api(venue, documents, _list_directory(manuscript_dir), model=args.model)
    except AutofillApiError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return None


@_validate
def _cmd_schema(args: argparse.Namespace) -> int:
    """Print a venue's field schema, including autofill roles, as JSON.

    Used by the autofill front-ends (the Claude skill and the API engine) to
    learn which fields to extract, classify, map to files, or leave alone,
    without duplicating that knowledge outside ``venues.json``.
    """
    from .autofill import field_schema

    try:
        venue = get_venue(args.venue)
    except KeyError:
        print(f"error: unknown venue '{args.venue}'", file=sys.stderr)
        print("Run 'paperpush --venues' to see supported venues.", file=sys.stderr)
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
        print("Run 'paperpush --venues' to see supported venues.", file=sys.stderr)
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
        epilog=("AI agents: run 'paperpush agent-guide' before driving this CLI.\n" f"Docs and issues: {__url__}"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"paperpush {__version__}")
    parser.add_argument("--venues", action="store_true", help="list supported venues and exit")

    # Verbosity is a per-command concern, so -v/-q live on each subcommand
    # (added via this shared parent) rather than at the top level.
    verbosity = argparse.ArgumentParser(add_help=False)
    verbosity.add_argument("-v", "--verbose", action="count", default=0, help="increase logging verbosity (-v for info, " "-vv for debug); overridden by " "PAPERPUSH_LOG_LEVEL")
    verbosity.add_argument("-q", "--quiet", action="store_true", help="only log errors")

    # metavar lists only the public commands; the internal 'schema' command is
    # registered below but deliberately left out so it does not appear in --help.
    sub = parser.add_subparsers(dest="command", metavar="{subfile,options,autofill,validate,login,submit,agent-guide}")

    p_subfile = sub.add_parser("subfile", parents=[verbosity], help="create a <venue>.sub submission template")
    p_subfile.add_argument("venue", help="venue slug, e.g. biorxiv")
    p_subfile.add_argument("-o", "--output", help="output path for the .sub file")
    p_subfile.add_argument("--fill-defaults", dest="fill_defaults", action="store_true", help="pre-populate fields with their default values (this is the default)")
    p_subfile.add_argument("--dont-fill-defaults", dest="fill_defaults", action="store_false", help="leave fields with default values empty instead of pre-populating them")
    p_subfile.add_argument("--force", action="store_true", help="overwrite an existing .sub file")
    p_subfile.set_defaults(func=_cmd_subfile, fill_defaults=True)

    p_options = sub.add_parser("options", parents=[verbosity], help="list the allowed values for a field (VENUE.FIELD)")
    p_options.add_argument("field", metavar="VENUE.FIELD", help="the field to list, e.g. arxiv.crosslist_categories")
    p_options.add_argument("path", nargs="*", metavar="PATH", help="for a drill-down field (e.g. nature.subject_level), the category " "names to descend before listing the next level")
    p_options.set_defaults(func=_cmd_options)

    p_autofill = sub.add_parser("autofill", parents=[verbosity], help="fill a .sub file from a directory of manuscript files")
    p_autofill.add_argument("subfile", help="the .sub file to fill (created from the filename's venue slug " "if it does not exist yet), e.g. biorxiv.sub")
    p_autofill.add_argument("-d", "--directory", required=True, metavar="MANUSCRIPTDIR", help="directory holding the manuscript, figures, and other files")
    p_autofill.add_argument(
        "--engine",
        choices=["manual", "api"],
        default="manual",
        help="manual: read proposed values from --values (default; used by the " "Claude skill); api: extract them with the Anthropic API ",
    )
    p_autofill.add_argument("--values", metavar="FILE", help="JSON file of proposed field values (required for --engine manual)")
    p_autofill.add_argument("--manuscript", metavar="FILE", help="(api) the manuscript file; inferred from the directory if omitted")
    p_autofill.add_argument("--title-page", dest="title_page", metavar="FILE", help="(api) a standalone title page with author details, if separate")
    p_autofill.add_argument("--supplement", metavar="FILE", help="(api) a supplementary materials file, if any")
    p_autofill.add_argument("--model", default="claude-opus-4-8", help="(api) Anthropic model to use (default: claude-opus-4-8)")
    p_autofill.add_argument("--min-confidence", choices=["low", "medium", "high"], default="low", help="do not write any value below this confidence (default: low)")
    p_autofill.add_argument("-o", "--output", help="write the filled file here instead of overwriting the .sub")
    p_autofill.add_argument("--force", action="store_true", help="overwrite --output if it already exists")
    p_autofill.add_argument("--dry-run", action="store_true", help="show what would be filled without writing the file")
    p_autofill.set_defaults(func=_cmd_autofill)

    p_validate = sub.add_parser("validate", parents=[verbosity], help="run the pre-submission checks on a .sub file")
    p_validate.add_argument("subfile", help="path to the .sub file to check, e.g. biorxiv.sub")
    p_validate.add_argument(
        "--dont-check-links",
        dest="check_links",
        action="store_false",
        help="skip checking that the URLs cited in the manuscript files are "
        "reachable. By default validate probes them and warns about broken "
        "links (404/gone, including still-private GitHub repos), which requires "
        "network access.",
    )
    p_validate.add_argument(
        "--dont-check-for-sensitive-info",
        dest="check_sensitive",
        action="store_false",
        help="skip scanning the referenced files for information not meant to "
        "be published. By default validate scans them for API keys, passwords, "
        "private keys, GPS coordinates in figures, editable-document links, and "
        "LaTeX source comments, nudges when no public code repository is linked, "
        "and reminds arXiv submitters to run arxiv_latex_cleaner on unclean "
        "source. Reported as advisory warnings.",
    )
    p_validate.set_defaults(func=_cmd_validate, check_links=True, check_sensitive=True)

    p_login = sub.add_parser("login", parents=[verbosity], help="store credentials for a venue submission system")
    p_login.add_argument("venue", nargs="?", help="venue slug, e.g. biorxiv (omit with --list)")
    p_login.add_argument("--list", action="store_true", help="list the venues you are logged in to with usernames, then exit")
    p_login.add_argument("-u", "--username", help="username or email (otherwise you are prompted or it looks " "for the PAPERPUSH_USERNAME environment variable)")
    p_login.add_argument(
        "--password",
        help="password (otherwise you are prompted or it looks for the "
        "PAPERPUSH_PASSWORD environment variable). WARNING: exposes "
        "the password as plain text in the process list and shell history",
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
    p_login.add_argument("--logout", action="store_true", help="remove stored credentials for the venue")
    p_login.add_argument(
        "--no-verify", dest="no_verify", action="store_true", help="store the credentials without checking them against " "the venue's sign-in (the default opens a browser and " "verifies first)"
    )
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
    p_submit.add_argument(
        "--debug", action="store_true", help="open the Playwright Inspector at the first step " "to walk the wizard line by line; reuses a saved " "session from an earlier run to skip sign-in"
    )
    p_submit.add_argument("--new-session", action="store_true", help="discard any saved browser session and sign in " "fresh (use after switching accounts)")
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
    p_submit.set_defaults(func=_cmd_submit)

    p_agent_guide = sub.add_parser("agent-guide", parents=[verbosity], help="print the guide for AI agents driving this CLI (AGENTS.md)")
    p_agent_guide.set_defaults(func=_cmd_agent_guide)

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

    if args.venues:
        return _print_venues()

    if not getattr(args, "command", None):
        parser.print_help()
        return 0

    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
