"""``paperpush login <venue> --browser``: the one-time sign-in page on localhost.

Covers the page itself (:mod:`paperpush.login_form`): its guards (loopback
``Host``, secret path, same-origin POST), the post/redirect/get flow, retries
after a failed check, the attempt cap and the timeout, and that the password
never reaches the printed output. Then the CLI wiring (storing a login,
verification failures shown on the page, flag conflicts), the headed
verification's terminal-less wait, and the MCP ``login(browser=True)`` path.

Every test drives the page over real HTTP on 127.0.0.1 from a helper thread;
no browser is opened (``open_browser=False`` / ``--no-open``).
"""

from __future__ import annotations

import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest

from paperpush import credentials, login_form, mcp_server
from paperpush.cli import main

SECRET = "hunter2&=%sure"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: D401 -- urllib's hook
        return None


def _request(url, data=None, headers=None, follow=True):
    """(status, body) for a GET, or a POST when ``data`` is given."""
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    request = urllib.request.Request(url, data=body, headers=headers or {})
    opener = urllib.request.build_opener() if follow else urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=10) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


class _Page:
    """Runs :func:`run_login_form` on a thread and exposes its URL and output."""

    def __init__(self, check, spec=None, **kwargs):
        self.lines: list[str] = []
        self.url = None
        self.outcome = None
        self._ready = threading.Event()
        spec = spec or login_form.LoginFormSpec(venue="biorxiv", venue_name="bioRxiv")

        def announce(line):
            self.lines.append(line)
            found = re.search(r"http://127\.0\.0\.1:\d+/\S+", line)
            if found:
                self.url = found.group(0)
                self._ready.set()

        def run():
            self.outcome = login_form.run_login_form(spec, check, open_browser=False, announce=announce, **kwargs)
            self._ready.set()

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
        assert self._ready.wait(10), "the page never printed its URL"

    def join(self):
        self.thread.join(20)
        assert not self.thread.is_alive()
        return self.outcome


@pytest.fixture(autouse=True)
def _fast_linger(monkeypatch):
    monkeypatch.setattr(login_form, "_LINGER_SECONDS", 0.2)


def _ok(attempt):
    return login_form.AttemptResult(True, f"Saved ({attempt.username}).")


# --- the page --------------------------------------------------------------


def test_page_collects_credentials_and_hands_them_to_the_check():
    seen = []

    def check(attempt):
        seen.append(attempt)
        return _ok(attempt)

    page = _Page(check)
    status, body = _request(page.url)
    assert status == 200 and "Sign in to bioRxiv" in body and "type=password" in body
    status, _ = _request(page.url, {"username": " ada@example.edu ", "password": SECRET}, follow=False)
    assert status == 303
    outcome = page.join()
    assert outcome.ok and outcome.attempts == 1
    assert seen[0].username == "ada@example.edu" and seen[0].password == SECRET and seen[0].method == "password"
    assert SECRET not in "\n".join(page.lines)
    assert SECRET not in repr(seen[0])


def test_page_serves_only_its_secret_path_on_loopback():
    page = _Page(_ok)
    base = page.url.rsplit("/", 1)[0]
    assert _request(base + "/guess")[0] == 404
    assert _request(page.url + "x")[0] == 404
    assert _request(page.url, headers={"Host": "attacker.example"})[0] == 403
    assert (
        _request(
            page.url,
            {"username": "a", "password": "b"},
            headers={"Origin": "http://attacker.example"},
        )[0]
        == 403
    )
    status, body = _request(page.url)
    assert status == 200
    _request(page.url, {"username": "a", "password": "b"})
    assert page.join().ok


def test_page_responses_are_not_cached_or_framed():
    page = _Page(_ok)
    with urllib.request.urlopen(page.url, timeout=10) as response:
        assert response.headers["Cache-Control"] == "no-store"
        assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
        assert response.headers["Referrer-Policy"] == "same-origin"
    _request(page.url, {"username": "a", "password": "b"})
    page.join()


def test_page_asks_again_for_a_missing_field_without_calling_the_check():
    calls = []

    def check(attempt):
        calls.append(attempt)
        return _ok(attempt)

    page = _Page(check)
    _, body = _request(page.url, {"username": "ada", "password": ""})
    assert "Enter your password." in body
    assert 'value="ada"' in body  # the username is kept
    assert not calls
    _request(page.url, {"username": "ada", "password": "pw"})
    assert page.join().ok and len(calls) == 1


def test_a_failed_check_puts_the_form_back_with_the_reason():
    results = iter(
        [
            login_form.AttemptResult(False, "wrong password"),
            login_form.AttemptResult(True, "Saved."),
        ]
    )
    page = _Page(lambda attempt: next(results))
    _request(page.url, {"username": "ada", "password": "bad"})
    for _ in range(100):
        _, body = _request(page.url)
        if "wrong password" in body:
            break
        time.sleep(0.05)
    assert "wrong password" in body and "type=password" in body
    _request(page.url, {"username": "ada", "password": "good"})
    outcome = page.join()
    assert outcome.ok and outcome.attempts == 2
    assert any("did not work: wrong password" in line for line in page.lines)


def test_the_page_closes_after_too_many_failures(monkeypatch):
    monkeypatch.setattr(login_form, "MAX_ATTEMPTS", 2)
    page = _Page(lambda attempt: login_form.AttemptResult(False, "nope"))
    for _ in range(2):
        for _ in range(100):
            if "type=password" in _request(page.url)[1]:
                break
            time.sleep(0.05)
        _request(page.url, {"username": "ada", "password": "x"})
    outcome = page.join()
    assert not outcome.ok and outcome.attempts == 2 and "2 sign-in attempts failed" in outcome.message


def test_a_final_failure_closes_the_page_at_once():
    page = _Page(lambda attempt: login_form.AttemptResult(False, "ORCID is not recorded for this venue.", final=True))
    _request(page.url, {"username": "ada", "password": "x"})
    outcome = page.join()
    assert not outcome.ok and outcome.attempts == 1 and "ORCID" in outcome.message


def test_the_page_times_out():
    outcome = _Page(_ok, timeout=0.5).join()
    assert not outcome.ok and outcome.timed_out


def test_a_check_that_raises_is_reported_not_fatal():
    results = iter([RuntimeError("no display"), None])

    def check(attempt):
        item = next(results)
        if isinstance(item, Exception):
            raise item
        return _ok(attempt)

    page = _Page(check)
    _request(page.url, {"username": "ada", "password": "x"})
    for _ in range(100):
        if "no display" in _request(page.url)[1]:
            break
        time.sleep(0.05)
    _request(page.url, {"username": "ada", "password": "x"})
    assert page.join().ok


def test_page_offers_methods_and_providers_and_rejects_others():
    spec = login_form.LoginFormSpec(
        venue="sra",
        venue_name="SRA",
        methods=("password", "orcid"),
        providers=("ncbi", "era"),
    )
    seen = []
    page = _Page(lambda a: (seen.append(a), _ok(a))[1], spec=spec)
    body = _request(page.url)[1]
    assert 'value="orcid"' in body and '<option value="era"' in body
    assert "Choose one of the sign-in methods" in _request(page.url, {"method": "github", "username": "a", "password": "b"})[1]
    assert "Choose one of the sign-in providers" in _request(page.url, {"provider": "aol", "username": "a", "password": "b"})[1]
    _request(
        page.url,
        {"method": "password", "provider": "ERA", "username": "a", "password": "b"},
    )
    assert page.join().ok
    assert seen[0].provider == "era"


def test_page_escapes_what_it_echoes():
    page = _Page(lambda a: login_form.AttemptResult(True, "ok"))
    body = _request(page.url, {"username": '"><script>x</script>', "password": ""})[1]
    assert "<script>x" not in body and "&lt;script&gt;" in body
    _request(page.url, {"username": "a", "password": "b"})
    page.join()


# --- the CLI ---------------------------------------------------------------


def _drive_cli(monkeypatch, argv, posts, *, wait_body=None):
    """Run ``main(argv)`` on a thread, POST each dict in ``posts`` to the page, return (rc, output)."""
    lines: list[str] = []
    url_ready = threading.Event()
    real_run = login_form.run_login_form

    def run_with_capture(spec, check, **kwargs):
        def announce(line):
            lines.append(line)
            found = re.search(r"http://127\.0\.0\.1:\d+/\S+", line)
            if found:
                lines.append(("URL", found.group(0)))
                url_ready.set()

        kwargs["open_browser"] = False
        return real_run(spec, check, announce=announce, **kwargs)

    monkeypatch.setattr(login_form, "run_login_form", run_with_capture)
    result = {}
    thread = threading.Thread(target=lambda: result.setdefault("rc", main(argv)), daemon=True)
    thread.start()
    assert url_ready.wait(10)
    url = next(item[1] for item in lines if isinstance(item, tuple))
    for data in posts:
        for _ in range(200):
            body = _request(url)[1]
            if "type=password" in body or "Signed in" in body:
                break
            time.sleep(0.05)
        _request(url, data)
    thread.join(20)
    assert not thread.is_alive()
    return result["rc"], [line for line in lines if isinstance(line, str)]


def test_cli_browser_login_stores_the_credential(monkeypatch, capsys):
    rc, lines = _drive_cli(
        monkeypatch,
        ["login", "biorxiv", "--browser", "--no-open", "--no-verify"],
        [{"username": "ada@example.edu", "password": SECRET}],
    )
    out = capsys.readouterr()
    assert rc == 0
    cred = credentials.get_credential("biorxiv")
    assert cred.username == "ada@example.edu" and cred.password == SECRET
    assert "Stored credentials for biorxiv (ada@example.edu)" in out.out
    assert SECRET not in out.out + out.err + "\n".join(lines)


def test_cli_browser_login_verifies_and_retries(monkeypatch, capsys):
    from paperpush.venues import login as login_mod

    tries = []

    def fake_verify(slug, username, password, **kwargs):
        tries.append((username, password, kwargs.get("method")))
        if password != "right":
            raise login_mod.LoginVerificationError("the sign-in did not take")

    monkeypatch.setattr(login_mod, "verify_login", fake_verify)
    rc, lines = _drive_cli(
        monkeypatch,
        ["login", "biorxiv", "--browser", "--no-open"],
        [
            {"username": "ada", "password": "wrong"},
            {"username": "ada", "password": "right"},
        ],
    )
    capsys.readouterr()
    assert rc == 0
    assert [t[1] for t in tries] == ["wrong", "right"]
    assert credentials.get_credential("biorxiv").password == "right"
    assert any("did not work: The sign-in did not work" in line for line in lines)


def test_cli_browser_login_with_orcid_validates_the_id(monkeypatch, capsys):
    rc, _ = _drive_cli(
        monkeypatch,
        ["login", "biorxiv", "--browser", "--no-open", "--no-verify"],
        [
            {"method": "orcid", "username": "not-an-id", "password": "x"},
            {"method": "orcid", "username": "0000-0002-1825-0097", "password": "x"},
        ],
    )
    capsys.readouterr()
    assert rc == 0
    cred = credentials.get_credential("biorxiv")
    assert cred.method == "orcid" and cred.orcid == "0000-0002-1825-0097"


def test_cli_browser_refuses_a_password_flag(capsys):
    assert main(["login", "biorxiv", "--browser", "--password", "x"]) == 2
    assert "drop --password" in capsys.readouterr().err


def test_cli_terminal_login_without_a_terminal_suggests_browser(monkeypatch, capsys):
    monkeypatch.delenv("PAPERPUSH_USERNAME", raising=False)
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(""))
    assert main(["login", "biorxiv", "--no-verify"]) == 1
    assert "--browser" in capsys.readouterr().err


# --- headed verification without a terminal --------------------------------


def test_verification_watches_the_window_when_there_is_no_terminal(monkeypatch):
    from paperpush.venues import common
    from paperpush.venues import login as login_mod

    watched = []
    monkeypatch.setattr(common, "_stdin_is_terminal", lambda: False)
    monkeypatch.setattr(
        common,
        "_watch_sign_in",
        lambda name, watch, page, check: watched.append(name) or True,
    )
    monkeypatch.setattr(
        login_mod,
        "wait_for_human",
        lambda prompt: pytest.fail("must not wait for Enter"),
    )

    class _Module:
        def is_logged_in(self, page, timeout_ms=None):
            return True

    with common.use_sign_in_watch(None, timeout=1):
        login_mod._wait_for_finish("biorxiv", object(), _Module())
    assert watched == ["biorxiv"]


def test_verification_waits_for_enter_without_a_watch(monkeypatch):
    from paperpush.venues import login as login_mod

    prompts = []
    monkeypatch.setattr(login_mod, "wait_for_human", lambda prompt: prompts.append(prompt) or True)
    login_mod._wait_for_finish("biorxiv", object(), object())
    assert prompts


# --- MCP -------------------------------------------------------------------


def test_mcp_login_offers_the_browser_command(monkeypatch):
    monkeypatch.delenv("PAPERPUSH_USERNAME", raising=False)
    monkeypatch.delenv("PAPERPUSH_PASSWORD", raising=False)
    result = mcp_server.login("biorxiv")
    assert result["status"] == "action_required"
    assert result["browser_command"] == "paperpush login biorxiv --browser"


def test_mcp_login_browser_starts_the_page_and_reports_progress(monkeypatch, tmp_path):
    monkeypatch.delenv("PAPERPUSH_USERNAME", raising=False)
    monkeypatch.delenv("PAPERPUSH_PASSWORD", raising=False)
    monkeypatch.setattr(mcp_server, "_LOGIN_RUNS", {})
    launched = []

    class _Process:
        pid = 4242
        returncode = None

        def poll(self):
            return self.returncode

    process = _Process()

    def fake_popen(command, stdout, **kwargs):
        launched.append((command, kwargs))
        stdout.write(b"Sign in to biorxiv on this page:\n  http://127.0.0.1:5555/tok\n")
        stdout.flush()
        return process

    monkeypatch.setattr(mcp_server.subprocess, "Popen", fake_popen)
    result = mcp_server.login("biorxiv", browser=True)
    assert result["status"] == "awaiting_sign_in"
    assert result["url"] == "http://127.0.0.1:5555/tok"
    command, kwargs = launched[0]
    assert command[-1] == "--browser" and kwargs["stdin"] is mcp_server.subprocess.DEVNULL

    # A second call returns the open page instead of starting another.
    again = mcp_server.login("biorxiv", browser=True)
    assert again["url"] == result["url"] and len(launched) == 1

    status = mcp_server.login_status("biorxiv")
    assert status["logged_in"] is False and status["login_progress"]["state"] == "waiting"

    credentials.save_credential("biorxiv", "ada", "pw")
    process.returncode = 0
    status = mcp_server.login_status("biorxiv")
    assert status["logged_in"] is True and status["login_progress"]["state"] == "finished"
    assert "biorxiv" not in mcp_server._LOGIN_RUNS


# --- a real browser ----------------------------------------------------------


def test_a_real_browser_can_submit_the_form():
    """Regression: curl sends no Origin, but a browser does -- and sends
    ``Origin: null`` under ``Referrer-Policy: no-referrer``, which the
    same-origin check refused. Drive the page in Chromium end to end."""
    from playwright.sync_api import sync_playwright

    seen = []
    page = _Page(lambda a: (seen.append(a), _ok(a))[1])
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            tab = browser.new_page()
            tab.goto(page.url)
            tab.fill("#username", "ada@example.edu")
            tab.fill("#password", SECRET)
            tab.click("button[type=submit]")
            tab.wait_for_selector("text=Signed in", timeout=10000)
        finally:
            browser.close()
    assert page.join().ok
    assert seen[0].password == SECRET
