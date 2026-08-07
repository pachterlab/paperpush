"""Tests for the ``paperpush submit`` command.

Two kinds of coverage live here:

* **Offline CLI wiring** -- ``submit`` loads a ``.sub`` file, validates it,
  looks up the venue's runner, signs in (driving the login flow when no
  credentials are stored), and finally invokes the runner. These tests stub the
  runner and the ``.sub`` load so they exercise that wiring without a browser or
  a network call, including a sweep over every committed sample ``.sub`` and a
  check that each sample's venue resolves to a real submission runner.

* **Live-portal walkthrough** -- the ``@pytest.mark.portal`` test at the bottom
  is the pytest version of the ``.vscode/launch.json`` "Debug <Venue>" entries:
  it runs the real ``submit`` against each venue's visible portal, one venue
  at a time, holding the browser open between venues. It is skipped by a bare
  ``pytest`` and needs ``--run-portal -s`` to run (see the walkthrough section).

Credential storage is isolated from the real keychain/config by the autouse
``_isolate_user_state`` fixture in ``conftest.py`` (except in the walkthrough,
which deliberately restores the developer's real state -- see
``_use_real_user_state``).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import sample_subfiles
from sample_subfiles import REPO_ROOT, scenarios

from paperpush import credentials, subfile, venues
from paperpush.cli import main
from paperpush.subfile import SubFile
from tests.submit_walkthrough_status import record_failure, record_success

# Sample submission files, base files first; shared by the offline sweep tests.
SCENARIOS = scenarios()
SUB_IDS = [s.filename[: -len(".sub")] for s in SCENARIOS]

_USERNAME = "researcher@example.edu"
_PASSWORD = "s3cret-token"


# --- offline CLI wiring: login handoff -------------------------------------


def _stub_submit(monkeypatch):
    """Make ``submit`` reach the credential check without a real browser.

    Stubs the .sub load, validation, and the venue runner so the command
    short-circuits the Playwright path. Returns a list the runner appends to
    when it is invoked, so a test can assert the submission actually ran.
    """
    import importlib

    import paperpush.subfile as subfile
    import paperpush.venues as venues

    # ``paperpush.validate`` the attribute is the public ``validate()`` function
    # (it shadows the submodule); fetch the real module to monkeypatch it.
    validate_mod = importlib.import_module("paperpush.validate")

    ran: list = []
    monkeypatch.setattr(subfile, "load", lambda path: SubFile(venue="biorxiv", values={}))
    monkeypatch.setattr(validate_mod, "validate", lambda sub, venue, **kwargs: [])
    monkeypatch.setattr(venues, "get_runner", lambda venue: (lambda *a, **k: ran.append((a, k))))
    return ran


def test_submit_logs_in_when_not_logged_in(monkeypatch, capsys):
    ran = _stub_submit(monkeypatch)
    # No stored credentials, so submit should drive the login flow. Feed the
    # username/password through the environment so nothing prompts.
    monkeypatch.setenv("PAPERPUSH_USERNAME", "researcher@example.edu")
    monkeypatch.setenv("PAPERPUSH_PASSWORD", "s3cret-token")

    rc = main(["submit", "biorxiv.sub"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "Not logged in to biorxiv" in out
    # The login actually stored credentials, and the runner still ran.
    assert credentials.get_credential("biorxiv") is not None
    assert ran


def test_submit_skips_login_when_already_logged_in(monkeypatch, capsys):
    ran = _stub_submit(monkeypatch)
    credentials.save_credential("biorxiv", "researcher@example.edu", "s3cret-token")

    rc = main(["submit", "biorxiv.sub"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "Not logged in" not in out
    assert ran


def test_submit_skips_login_for_loginless_venue(monkeypatch, capsys):
    ran = _stub_submit(monkeypatch)

    monkeypatch.setattr(venues, "login_required", lambda slug: False)
    rc = main(["submit", "biorxiv.sub"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "does not require login" in out
    assert credentials.get_credential("biorxiv") is None
    assert ran


# --- offline CLI wiring: sweep every committed sample ----------------------


@pytest.mark.parametrize("scenario", SCENARIOS, ids=SUB_IDS)
def test_submit_command_for_each_subfile(scenario, monkeypatch):
    """``paperpush submit <file>`` validates then invokes the runner.

    The submission runner is stubbed so no browser opens; credentials are
    pre-stored under the submission-base slug so submit does not divert into the
    login flow. Asserts the command reaches the runner and hands it the parsed
    field values.
    """
    monkeypatch.chdir(REPO_ROOT)

    ran: list = []
    monkeypatch.setattr(venues, "get_runner", lambda slug: (lambda *a, **k: ran.append((a, k))))

    base = venues.submission_base(scenario.venue)
    credentials.save_credential(base, _USERNAME, _PASSWORD)

    rc = main(["submit", str(scenario.path)])

    assert rc == 0
    assert ran, f"submit did not invoke the runner for {scenario.filename}"
    call_args, _call_kwargs = ran[0]
    # The runner is called with the .sub's parsed field values as its first arg.
    assert call_args and isinstance(call_args[0], dict) and call_args[0]


@pytest.mark.parametrize("scenario", SCENARIOS, ids=SUB_IDS)
def test_venue_has_submission_runner(scenario):
    # 'paperpush submit' must be able to dispatch this venue to a runner.
    assert callable(venues.get_runner(scenario.venue))


# --- runner-side cross-field guards ----------------------------------------
#
# A runner rejects a value combination the per-field metadata cannot express
# (validate() checks fields one at a time). These guards run before any browser
# is opened, so they are testable without Playwright.


def _discrete_mathematics_runner():
    from paperpush.venues.discrete_mathematics.discrete_mathematics import \
        DiscreteMathematicsVenue

    return DiscreteMathematicsVenue()


@pytest.mark.parametrize(
    "missing",
    ["data_repository_name", "data_repository_url", "source_of_data", "dataset_title"],
)
def test_discrete_mathematics_share_data_requires_repository_fields(missing):
    values = {
        "share_data": "yes",
        "data_repository_name": "Mendeley Data",
        "data_repository_url": "https://data.mendeley.com/datasets/example",
        "source_of_data": "original",
        "dataset_title": "A dataset",
    }
    values[missing] = ""
    with pytest.raises(ValueError, match=missing):
        _discrete_mathematics_runner().submit(values)


def test_discrete_mathematics_share_data_rejects_unknown_source_of_data():
    values = {
        "share_data": "yes",
        "data_repository_name": "Mendeley Data",
        "data_repository_url": "https://data.mendeley.com/datasets/example",
        "source_of_data": "borrowed",
        "dataset_title": "A dataset",
    }
    with pytest.raises(ValueError, match="original"):
        _discrete_mathematics_runner().submit(values)


def test_discrete_mathematics_declining_requires_a_data_statement():
    with pytest.raises(ValueError, match="data_statement"):
        _discrete_mathematics_runner().submit({"share_data": "no", "data_statement": ""})


def test_combinatorica_requires_the_author_agreement_before_browser_launch():
    from paperpush.venues.editflow.combinatorica import CombinatoricaVenue

    with pytest.raises(ValueError, match="author_agreement"):
        CombinatoricaVenue().submit({"author_agreement": "no"})


@pytest.mark.parametrize("value", ["05", "5C05", "05-05", "05C050"])
def test_combinatorica_rejects_malformed_msc_classification(value):
    from paperpush.venues.editflow.combinatorica import validate_msc_classification

    with pytest.raises(ValueError, match="five-character MSC"):
        validate_msc_classification(value)


# arXiv's license radios carry the license URI as their value, so the runner
# maps the .sub's license name onto one before it opens a browser.


def _arxiv_license_url(name):
    from paperpush.venues.arxiv.arxiv import license_url

    return license_url(name)


def test_arxiv_maps_every_declared_license_to_a_distinct_uri():
    field = next(f for f in venues.get_venue_impl("arxiv")._VENUE.fields if f.id == "license")
    urls = {option: _arxiv_license_url(option) for option in field.options}
    assert all(url.endswith("/") and "://" in url for url in urls.values())
    assert len(set(urls.values())) == len(field.options), f"licenses share a URI: {urls}"
    assert urls["CC BY 4.0"] == "http://creativecommons.org/licenses/by/4.0/"
    assert urls["arXiv.org perpetual, non-exclusive license 1.0"] == "http://arxiv.org/licenses/nonexclusive-distrib/1.0/"


@pytest.mark.parametrize(
    "written, expected",
    [
        ("CC-BY-NC-ND", "CC BY-NC-ND 4.0"),
        ("cc_by_sa", "CC BY-SA 4.0"),
        (" CC0 ", "CC0 1.0"),
        ("arXiv", "arXiv.org perpetual, non-exclusive license 1.0"),
        # Unset falls back to the venue default, arXiv's own license.
        ("", "arXiv.org perpetual, non-exclusive license 1.0"),
    ],
)
def test_arxiv_accepts_license_shorthands(written, expected):
    assert _arxiv_license_url(written) == _arxiv_license_url(expected)


def test_arxiv_rejects_an_unknown_license():
    # Better to stop here than to post the paper under a license nobody chose.
    with pytest.raises(ValueError, match="MIT"):
        _arxiv_license_url("MIT")


def test_every_venue_has_a_sample_subfile():
    """Guard: scenarios resolve to committed files on disk for the sweep above."""
    assert SCENARIOS, "no sample scenarios discovered"
    for scenario in SCENARIOS:
        assert scenario.path.exists(), f"missing committed sample {scenario.relpath}; regenerate with " "'python tests/sample_subfiles.py'"
    assert sample_subfiles.SAMPLES_DIR.is_dir()


# --- live-portal walkthrough -----------------------------------------------
#
# The ``.vscode/launch.json`` "Debug <Venue>" entries each just run
#
#     paperpush submit tests/sub_files/<venue>/<venue>.sub
#
# against the real portal in a visible browser. The parametrized test below runs
# the same thing, but as one pytest so a single command walks every venue in
# turn. Each venue's runner ends in ``paperpush.venues.common.hold_open``,
# which keeps the browser open and blocks on ``input()`` until you press
# **Ctrl+D** (EOF) or **Ctrl+C**; that is your chance to inspect the page before
# the next venue starts.
#
# Because it drives live portals it is marked ``@pytest.mark.portal`` and is
# skipped by a bare ``pytest``. It also needs ``-s`` so ``hold_open`` can read
# your terminal -- with output captured, ``input()`` sees EOF immediately and the
# window will not pause.
#
# Run the whole walkthrough (sign in by hand in each browser as needed)::
#
#     pytest tests/test_submit.py --run-portal -s
#
# Run a single venue by exact slug::
#
#     pytest tests/test_submit.py --run-portal -s --venue nature
#
# Prefer ``--venue`` over ``-k`` for selecting one venue: ``-k`` matches
# substrings, so ``-k bioinformatics`` would also run ``bmc_bioinformatics``,
# whereas ``--venue bioinformatics`` matches the slug exactly. ``--venue``
# is repeatable to run a chosen few (``--venue nature --venue biorxiv``).
#
# Each venue defaults to its canonical ``<slug>/<slug>.sub``. To walk a
# specific .sub instead (e.g. arxiv's LaTeX-bundle variant), pass ``--subfile``
# (repeatable); the venue is read from the file itself::
#
#     pytest tests/test_submit.py --run-portal -s \
#         --subfile tests/sub_files/arxiv/arxiv_tex.sub
#
# Unlike the rest of the suite, this walkthrough reuses your *real* saved
# sessions and credentials (saved by a prior ``paperpush submit`` /
# ``paperpush login``) rather than the isolated tmp config, so sign-in is
# automatic exactly like the launch.json configs. See ``_use_real_user_state``.

# Captured at import time, before conftest's autouse isolation fixture has had a
# chance to override it, so we can restore the developer's genuine config (and
# thus their saved portal sessions) for this walkthrough.
_REAL_XDG_CONFIG_HOME = os.environ.get("XDG_CONFIG_HOME")
_REAL_KEYRING = os.environ.get("PAPERPUSH_KEYRING")

SUB_FILES_DIR = Path(__file__).parent / "sub_files"


def _walkthrough_venues() -> list[str]:
    """Venues with a canonical ``<slug>/<slug>.sub`` fixture and a runner.

    Discovered from the fixtures on disk (kept in sync automatically) rather
    than hardcoded, and filtered to those that have a registered submission
    runner -- so launch.json's runner-less entries (plos_compbio,
    bmc_bioinformatics, nucleic_acids_research) are simply not collected.
    """
    slugs = []
    for sub_dir in sorted(SUB_FILES_DIR.iterdir()):
        if not sub_dir.is_dir():
            continue
        slug = sub_dir.name
        if not (sub_dir / f"{slug}.sub").is_file():
            continue
        try:
            venues.get_runner(slug)
        except KeyError:
            continue
        slugs.append(slug)
    return slugs


def _walkthrough_params(config):
    """``(slug, sub_path, id)`` triples the submit walkthrough runs.

    By default, the canonical ``<slug>/<slug>.sub`` for every discovered
    walkthrough venue -- with the id kept equal to the slug so existing node
    ids (``test_submit_walkthrough[arxiv]``) and ``-k`` selections are stable.

    With ``--subfile PATH`` (repeatable), exactly those files instead: each is
    mapped to its venue by reading the ``.sub`` itself, so ``--venue`` still
    filters and ``record_success`` stamps the right venue. The id is the file
    stem, disambiguating two subs of the same venue (``arxiv`` vs ``arxiv_tex``).
    """
    chosen = config.getoption("--subfile")
    if chosen:
        params = []
        for raw in dict.fromkeys(chosen):  # de-dupe, preserve order
            path = Path(raw)
            if not path.is_file():
                raise pytest.UsageError(f"--subfile: no such file {raw!r}")
            try:
                slug = subfile.load(path).venue
            except Exception as exc:  # malformed .sub -> clear message, not a traceback
                raise pytest.UsageError(f"--subfile: could not read venue from {raw!r}: {exc}")
            params.append((slug, path, path.stem))
        return params
    return [(slug, SUB_FILES_DIR / slug / f"{slug}.sub", slug) for slug in _walkthrough_venues()]


def pytest_generate_tests(metafunc):
    """Parametrize the walkthrough over ``(slug, sub_path)``.

    Done here rather than with a static ``@pytest.mark.parametrize`` because the
    walkthrough venues are discovered from the fixtures on disk (or given
    explicitly via ``--subfile``). Parametrizing ``slug`` and ``sub_path``
    together keeps the venue slug in ``callspec.params`` so the ``--venue``
    filter in ``conftest.py`` (exact match, so ``--venue bioinformatics`` never
    also runs ``bmc_bioinformatics``) keeps working; the guard leaves the
    ``scenario`` sweep above untouched.
    """
    if "slug" not in metafunc.fixturenames:
        return
    params = _walkthrough_params(metafunc.config)
    metafunc.parametrize(
        "slug,sub_path",
        [(slug, path) for slug, path, _ in params],
        ids=[i for _, _, i in params],
    )


@pytest.fixture
def _use_real_user_state(_isolate_user_state, monkeypatch):
    """Undo conftest's tmp-config isolation for the walkthrough.

    Depends on ``_isolate_user_state`` so it runs *after* it and overrides its
    env changes. Restoring the real ``XDG_CONFIG_HOME`` (and keyring setting)
    means ``submit`` finds the browser sessions saved by ``paperpush
    session``/``login``, so sign-in is automatic just as it is from launch.json.
    """
    if _REAL_XDG_CONFIG_HOME is None:
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    else:
        monkeypatch.setenv("XDG_CONFIG_HOME", _REAL_XDG_CONFIG_HOME)
    if _REAL_KEYRING is None:
        monkeypatch.delenv("PAPERPUSH_KEYRING", raising=False)
    else:
        monkeypatch.setenv("PAPERPUSH_KEYRING", _REAL_KEYRING)


@pytest.mark.portal
@pytest.mark.usefixtures("_use_real_user_state")
def test_submit_walkthrough(slug, sub_path, capsys):
    """Open ``sub_path`` in a visible browser and hold the window open.

    Identical to the "Debug <Venue>" launch.json config: it runs the real
    ``submit`` (validation, sign-in, wizard, stop before final submit) and the
    runner's ``hold_open`` pauses on this venue until you press Ctrl+D/Ctrl+C,
    at which point pytest advances to the next venue. ``sub_path`` is the
    venue's canonical ``<slug>/<slug>.sub`` unless overridden with ``--subfile``.
    """
    sub = sub_path
    # Headed by default (matching launch.json), so a developer can watch and
    # sign in. An unattended runner has no display, so
    # PAPERPUSH_WALKTHROUGH_HEADLESS=1 switches to headless.
    # Either way, without -s pytest's captured stdin makes hold_open's input()
    # raise OSError, which hold_open swallows and returns -- so reaching
    # hold_open counts as success. With -s it instead waits for Ctrl+D/Ctrl+C.
    argv = ["submit", str(sub)]
    if os.environ.get("PAPERPUSH_WALKTHROUGH_HEADLESS") == "1":
        argv.append("--headless")
    # Portals fail intermittently (a flaky network hop, a slow page, a transient
    # 5xx), so give each venue up to three attempts before believing it is broken
    # -- only a venue that fails all three flips to ❌. An attempt fails if
    # ``submit`` exits non-zero or raises; the first clean exit wins and stops the
    # retries.
    max_attempts = 3

    # Print outside pytest's capture so it is visible even without -s; with -s
    # (required for hold_open) it simply lands inline before the browser opens.
    #
    # Record the outcome either way so the README's "Submit walkthrough" column
    # tracks reality: a clean run stamps today's date (✅), while failing every
    # attempt drops the venue (❌). Recording happens before the assert so a
    # failing run still flips the venue to ❌; the README is regenerated
    # separately via scripts/gen_readme_venues.py, which reads this status file.
    with capsys.disabled():
        print(f"\n=== submit {slug}: {sub} ===")
        print("    (the browser holds open at the end; press Ctrl+D to go on)")
        rc = None
        last_error = None
        for attempt in range(1, max_attempts + 1):
            try:
                rc = main(argv)
            except Exception as exc:  # a crash is a failed attempt, not yet fatal
                rc, last_error = None, exc
                print(f"    attempt {attempt}/{max_attempts} for {slug} crashed: {exc!r}")
            else:
                if rc == 0:
                    break
                last_error = None
                print(f"    attempt {attempt}/{max_attempts} for {slug} exited {rc}")
            if attempt < max_attempts:
                print(f"    retrying {slug} ...")
        if rc == 0:
            date = record_success(slug)
            print(f"    recorded submit walkthrough for {slug}: {date} (✅)")
        else:
            record_failure(slug)
            print(f"    recorded submit walkthrough FAILURE for {slug} after {max_attempts} attempts (❌)")
    if rc != 0:
        msg = f"submit {slug} failed all {max_attempts} attempts"
        if last_error is not None:
            raise AssertionError(f"{msg}; last error: {last_error!r}") from last_error
        raise AssertionError(msg)
