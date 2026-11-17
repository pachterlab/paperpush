"""A one-time sign-in page on localhost, for ``paperpush login <venue> --browser``.

``paperpush login`` normally asks for the username and password in the
terminal. An AI agent driving paperpush has no terminal to type into, and
anything typed into its chat goes into the model's context and the transcript.
This module gives the agent a third way: it starts the login, which serves a
small form on ``127.0.0.1``, and the author types the credentials into that
page in their own browser. The password goes straight from the browser to
this process, which checks it against the venue's real sign-in and stores it in
the OS keyring (see :mod:`paperpush.credentials`). Nothing secret is printed:
the agent sees only the page's URL and whether the login was stored.

What this protects is the conversation, not the machine. An agent with shell
access as the author could still read the keyring; what it never does is see
the password in its context.

Safeguards:

* the server listens on the loopback interface only;
* the page lives at a random, unguessable path (:func:`secrets.token_urlsafe`),
  and every other path is a 404;
* the ``Host`` header must name the loopback address (against DNS rebinding),
  and a POST must come from the page's own origin;
* responses are ``no-store`` with a strict Content-Security-Policy, and the
  request log is silenced so no field ever reaches stderr;
* the page stops being served once a login is stored, after
  :data:`MAX_ATTEMPTS` failed tries, or after the timeout.

The sign-in check (Playwright) runs on the calling thread, never on a request
thread: a POST queues the credentials and the page polls until the check is
done.
"""

from __future__ import annotations

import hmac
import html
import logging
import queue
import secrets
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional
from urllib.parse import parse_qs

logger = logging.getLogger(__name__)

#: How long the page waits for the author before giving up, in seconds.
DEFAULT_FORM_TIMEOUT_SECONDS = 15 * 60
#: Failed sign-in checks allowed before the page closes.
MAX_ATTEMPTS = 5
#: The largest POST body accepted, in bytes.
_MAX_BODY = 16 * 1024
#: How long the success page stays reachable after a login is stored, so the
#: author's browser can fetch it before the server shuts down.
_LINGER_SECONDS = 5.0

#: Display labels for the sign-in methods.
METHOD_LABELS = {
    "password": "Venue account",  # nosec B105 - display label, not a credential
    "orcid": "ORCID",
    "github": "GitHub",
}
_USERNAME_LABELS = {
    "password": "Username or email",  # nosec B105 - display label, not a credential
    "orcid": "ORCID iD or email",
    "github": "GitHub username or email",
}
_PASSWORD_LABELS = {
    "password": "Password",  # nosec B105 - display label, not a credential
    "orcid": "ORCID password",
    "github": "GitHub password",
}


@dataclass(frozen=True)
class LoginFormSpec:
    """What the page asks for.

    ``methods`` lists the sign-in methods to offer (the first is preselected);
    one entry means no choice is shown. ``providers`` are a venue's identity
    providers (NCBI's gateway), offered as a list when non-empty, with ``""`` for
    the venue's default.
    """

    venue: str
    venue_name: str
    methods: tuple[str, ...] = ("password",)
    providers: tuple[str, ...] = ()
    provider: str = ""
    username: str = ""
    submission_url: str = ""
    verifying: bool = True


@dataclass(frozen=True)
class Attempt:
    """One set of credentials the author sent from the page."""

    method: str
    username: str
    password: str = field(repr=False)
    provider: str = ""


@dataclass(frozen=True)
class AttemptResult:
    """What the caller's check made of an :class:`Attempt`.

    ``message`` is shown on the page and printed, so it must never contain the
    password. ``final`` ends the page even on failure (an error that retrying
    cannot fix, such as a venue whose ORCID flow is not recorded).
    """

    ok: bool
    message: str
    final: bool = False


@dataclass
class FormOutcome:
    """How the page ended: a stored login, the author gave up, or it timed out."""

    ok: bool
    message: str
    attempts: int = 0
    timed_out: bool = False


class _State:
    """The page's state, shared between the request threads and the caller."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.phase = "form"  # form | checking | done | closed
        self.error = ""
        self.message = ""
        self.username = ""
        self.method = ""
        self.provider = ""

    def snapshot(self) -> tuple[str, str, str, str, str, str]:
        with self.lock:
            return (
                self.phase,
                self.error,
                self.message,
                self.username,
                self.method,
                self.provider,
            )


def run_login_form(
    spec: LoginFormSpec,
    check: Callable[[Attempt], AttemptResult],
    *,
    port: int = 0,
    open_browser: bool = True,
    timeout: float = DEFAULT_FORM_TIMEOUT_SECONDS,
    announce: Optional[Callable[[str], None]] = None,
) -> FormOutcome:
    """Serve the sign-in page until a login is stored, then return.

    ``check`` is called on this thread with each :class:`Attempt` the author
    sends; it verifies and stores the credentials and returns an
    :class:`AttemptResult`. A failed result puts the form back up with the
    message, until :data:`MAX_ATTEMPTS`. ``announce`` gets the lines meant for
    the person or agent watching the command (the URL, each failed try); they
    never hold the password.

    ``port`` 0 picks a free port; pass a fixed one to forward it over SSH.
    ``timeout`` 0 waits forever.
    """
    if announce is None:
        # Flushed: an agent reads this from a pipe while the command is still running.
        def announce(line: str) -> None:
            print(line, flush=True)

    token = secrets.token_urlsafe(24)
    state = _State()
    state.username = spec.username
    state.method = spec.methods[0]
    state.provider = spec.provider
    submissions: "queue.Queue[Attempt]" = queue.Queue()

    server = ThreadingHTTPServer(("127.0.0.1", port), _make_handler(spec, token, state, submissions))
    server.daemon_threads = True
    bound_port = server.server_address[1]
    url = f"http://127.0.0.1:{bound_port}/{token}"
    thread = threading.Thread(target=server.serve_forever, name="paperpush-login-form", daemon=True)
    thread.start()

    announce(f"Sign in to {spec.venue} on this page, which is served only on this computer:")
    announce(f"  {url}")
    opened = False
    if open_browser:
        try:
            opened = webbrowser.open(url, new=2)
        except Exception as exc:  # noqa: BLE001 -- no browser is not an error; the URL is printed
            logger.debug("Could not open a browser for the login form: %s", exc)
    if opened:
        announce("A browser tab with the page should have opened. Your password goes from that page to paperpush only; it is never printed here.")
    else:
        announce("Open it in a browser on this computer. (Working over SSH? Forward the port first: " f"ssh -L {bound_port}:127.0.0.1:{bound_port} <host>, then open the same URL on your own machine.)")
    minutes = (f"{timeout / 60:g} minute" + ("" if timeout == 60 else "s")) if timeout else "no time limit"
    announce(f"Waiting for the sign-in ({minutes})…")

    deadline = time.monotonic() + timeout if timeout else None
    attempts = 0
    try:
        while True:
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                with state.lock:
                    state.phase = "closed"
                    state.error = "This page timed out. Start the login again."
                return FormOutcome(
                    False,
                    f"no sign-in arrived within {minutes}",
                    attempts,
                    timed_out=True,
                )
            try:
                attempt = submissions.get(timeout=min(1.0, remaining) if remaining is not None else 1.0)
            except queue.Empty:
                continue
            attempts += 1
            logger.info(
                "login form: checking attempt %d for %s (%s)",
                attempts,
                spec.venue,
                attempt.method,
            )
            try:
                result = check(attempt)
            except Exception as exc:  # noqa: BLE001 -- an unexpected failure is shown, not a crash of the page
                logger.warning("login form: the sign-in check raised %s", exc)
                result = AttemptResult(False, f"the sign-in check failed: {exc}")
            del attempt
            if result.ok:
                with state.lock:
                    state.phase = "done"
                    state.message = result.message
                # Let the polling page pick up the success before the server stops.
                linger_until = time.monotonic() + _LINGER_SECONDS
                while time.monotonic() < linger_until:
                    time.sleep(0.1)
                return FormOutcome(True, result.message, attempts)
            announce(f"Sign-in attempt {attempts} did not work: {result.message}")
            if result.final or attempts >= MAX_ATTEMPTS:
                reason = result.message if result.final else f"{attempts} sign-in attempts failed"
                with state.lock:
                    state.phase = "closed"
                    state.error = f"{result.message} This page is now closed; start the login again to retry."
                time.sleep(_LINGER_SECONDS)
                return FormOutcome(False, reason, attempts)
            with state.lock:
                state.phase = "form"
                state.error = result.message
    finally:
        server.shutdown()
        server.server_close()


def _make_handler(spec: LoginFormSpec, token: str, state: _State, submissions: "queue.Queue[Attempt]"):
    path = f"/{token}"

    class Handler(BaseHTTPRequestHandler):
        server_version = "paperpush-login"
        sys_version = ""

        def log_message(self, format, *args):  # noqa: A002 -- BaseHTTPRequestHandler's signature
            # Never log requests: the default writes the request line to stderr.
            return

        # -- guards -------------------------------------------------------
        def _allowed_hosts(self) -> set[str]:
            port = self.server.server_address[1]
            return {f"127.0.0.1:{port}", f"localhost:{port}"}

        def _guard(self) -> bool:
            if self.headers.get("Host", "") not in self._allowed_hosts():
                self._send(HTTPStatus.FORBIDDEN, "<p>Forbidden.</p>")
                return False
            if not hmac.compare_digest(self.path.split("?", 1)[0].encode(), path.encode()):
                self._send(HTTPStatus.NOT_FOUND, "<p>Not found.</p>")
                return False
            return True

        # -- verbs --------------------------------------------------------
        def do_GET(self):  # noqa: N802 -- http.server naming
            if not self._guard():
                return
            self._send(HTTPStatus.OK, _render(spec, state))

        def do_POST(self):  # noqa: N802 -- http.server naming
            if not self._guard():
                return
            origin = self.headers.get("Origin")
            if origin is not None and origin.removeprefix("http://") not in self._allowed_hosts():
                self._send(HTTPStatus.FORBIDDEN, "<p>Forbidden.</p>")
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = -1
            if length < 0 or length > _MAX_BODY:
                self._send(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "<p>Too large.</p>")
                return
            form = parse_qs(
                self.rfile.read(length).decode("utf-8", errors="replace"),
                keep_blank_values=True,
            )

            def one(name: str) -> str:
                values = form.get(name) or [""]
                return values[0]

            method = one("method") or spec.methods[0]
            username = one("username").strip()
            password = one("password")
            provider = one("provider").strip().casefold() if spec.providers else spec.provider
            error = ""
            if method not in spec.methods:
                error = "Choose one of the sign-in methods offered."
            elif spec.providers and provider and provider not in spec.providers:
                error = "Choose one of the sign-in providers offered."
            elif not username:
                error = "Enter your username."
            elif not password:
                error = "Enter your password."
            with state.lock:
                if state.phase != "form":
                    error = ""  # a second submit while checking: just show the status
                elif error:
                    state.error = error
                    state.username, state.method, state.provider = (
                        username,
                        method,
                        provider,
                    )
                else:
                    state.phase = "checking"
                    state.error = ""
                    state.username, state.method, state.provider = (
                        username,
                        method,
                        provider,
                    )
                    submissions.put(
                        Attempt(
                            method=method,
                            username=username,
                            password=password,
                            provider=provider,
                        )
                    )
            del password, form
            # Post/redirect/get, so a reload never resends the password.
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", path)
            self._security_headers()
            self.send_header("Content-Length", "0")
            self.end_headers()

        # -- output -------------------------------------------------------
        def _security_headers(self) -> None:
            self.send_header("Cache-Control", "no-store")
            # same-origin, not no-referrer: with no-referrer, browsers send `Origin: null`
            # on the form POST, which the same-origin check above would refuse.
            self.send_header("Referrer-Policy", "same-origin")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
            )

        def _send(self, status: HTTPStatus, body: str) -> None:
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self._security_headers()
            self.end_headers()
            self.wfile.write(data)

    return Handler


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------

_STYLE = """
:root { color-scheme: light dark; --bg: #f6f7f9; --card: #fff; --ink: #1d232b; --muted: #5d6773;
  --line: #d5dae0; --accent: #2457c5; --err-bg: #fdecec; --err: #9b1c1c; --ok: #1d6b38; }
@media (prefers-color-scheme: dark) { :root { --bg: #14171b; --card: #1d2228; --ink: #e8ebee;
  --muted: #9aa4ae; --line: #343c45; --accent: #7ea2ff; --err-bg: #3a1f22; --err: #ffb4b4; --ok: #8fdca8; } }
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--ink); font: 16px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 420px; margin: 8vh auto; padding: 0 16px; }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 24px; }
h1 { font-size: 1.25rem; margin: 0 0 4px; }
p.sub { color: var(--muted); margin: 0 0 20px; font-size: .9rem; }
label { display: block; font-weight: 600; font-size: .9rem; margin: 14px 0 4px; }
input[type=text], input[type=password], select { width: 100%; padding: 9px 10px; font: inherit; color: inherit;
  background: transparent; border: 1px solid var(--line); border-radius: 6px; }
fieldset { border: 0; padding: 0; margin: 0; }
legend { font-weight: 600; font-size: .9rem; margin: 0 0 4px; padding: 0; }
.methods label { display: inline-flex; gap: 6px; align-items: center; font-weight: 400; margin: 0 16px 0 0; }
button { margin-top: 20px; width: 100%; padding: 10px; font: inherit; font-weight: 600; color: #fff;
  background: var(--accent); border: 0; border-radius: 6px; cursor: pointer; }
.error { background: var(--err-bg); color: var(--err); border-radius: 6px; padding: 10px 12px; margin: 0 0 12px; font-size: .9rem; }
.ok { color: var(--ok); font-weight: 600; }
.note { color: var(--muted); font-size: .8rem; margin: 16px 0 0; }
"""


def _page(title: str, body: str, refresh: bool = False) -> str:
    meta = '<meta http-equiv="refresh" content="2">' if refresh else ""
    return "<!doctype html><html lang=en><head><meta charset=utf-8>" '<meta name=viewport content="width=device-width, initial-scale=1">' f"{meta}<title>{html.escape(title)}</title><style>{_STYLE}</style></head>" f"<body><main><div class=card>{body}</div></main></body></html>"


def _render(spec: LoginFormSpec, state: _State) -> str:
    phase, error, message, username, method, provider = state.snapshot()
    venue = html.escape(spec.venue_name or spec.venue)
    title = f"Sign in to {spec.venue_name or spec.venue} — paperpush"
    if phase == "done":
        return _page(
            title,
            f"<h1>Signed in</h1><p class=ok>{html.escape(message)}</p><p class=sub>You can close this tab and go back to your terminal or agent.</p>",
        )
    if phase == "closed":
        return _page(
            title,
            f"<h1>Sign-in page closed</h1><p class=error>{html.escape(error)}</p>",
        )
    if phase == "checking":
        return _page(
            title,
            f"<h1>Checking your {venue} sign-in…</h1>" "<p class=sub>paperpush is signing in to the portal with these details before it saves them. " "If a browser window opens and asks for a CAPTCHA or a two-factor code, finish it there. " "This page updates by itself.</p>",
            refresh=True,
        )

    parts = [
        f"<h1>Sign in to {venue}</h1>",
        "<p class=sub>For paperpush, which signs in to the submission portal for you. " "The details are saved on this computer only.</p>",
    ]
    if error:
        parts.append(f"<p class=error role=alert>{html.escape(error)}</p>")
    parts.append("<form method=post autocomplete=on>")
    if len(spec.methods) > 1:
        radios = "".join(f'<label><input type=radio name=method value="{html.escape(m)}"{" checked" if m == method else ""}> {html.escape(METHOD_LABELS.get(m, m))}</label>' for m in spec.methods)
        parts.append(f"<fieldset class=methods><legend>Sign in with</legend>{radios}</fieldset>")
        user_label = "Username, email, or ORCID iD" if "orcid" in spec.methods else "Username or email"
        pass_label = "Password (of the account chosen above)"  # nosec B105 - display label, not a credential
    else:
        parts.append(f'<input type=hidden name=method value="{html.escape(spec.methods[0])}">')
        user_label = _USERNAME_LABELS.get(spec.methods[0], "Username")
        pass_label = _PASSWORD_LABELS.get(spec.methods[0], "Password")
    if spec.providers:
        options = '<option value="">Default</option>' + "".join(f'<option value="{html.escape(p)}"{" selected" if p == provider else ""}>{html.escape(p)}</option>' for p in spec.providers)
        parts.append(f"<label for=provider>Account provider</label><select id=provider name=provider>{options}</select>")
    parts.append(f"<label for=username>{html.escape(user_label)}</label>" f'<input type=text id=username name=username value="{html.escape(username)}" autocomplete=username autocapitalize=none spellcheck=false required autofocus>')
    parts.append(f"<label for=password>{html.escape(pass_label)}</label>" "<input type=password id=password name=password autocomplete=current-password required>")
    parts.append("<button type=submit>Sign in</button></form>")
    notes = []
    if spec.verifying:
        notes.append("paperpush checks these details by signing in to the portal before saving them.")
    if spec.submission_url:
        url = html.escape(spec.submission_url)
        notes.append(f'No account yet? Register at <a href="{url}" rel=noreferrer target=_blank>{url}</a> first.')
    if notes:
        parts.append(f"<p class=note>{' '.join(notes)}</p>")
    return _page(title, "".join(parts))
