"""Unit tests for :mod:`paperpush.net_guard`, the public server's SSRF guard.

No request leaves the machine: the refused addresses are refused before any
connection is made, and DNS answers are faked with ``socket.getaddrinfo``.
"""

from __future__ import annotations

import socket
import urllib.error

import pytest

from paperpush import net_guard


@pytest.mark.parametrize(
    "address, public",
    [
        ("8.8.8.8", True),
        ("2606:4700:4700::1111", True),
        ("127.0.0.1", False),
        ("10.1.2.3", False),
        ("172.16.0.1", False),
        ("192.168.1.1", False),
        ("169.254.169.254", False),  # cloud metadata
        ("100.64.0.1", False),  # carrier-grade NAT
        ("0.0.0.0", False),
        ("::1", False),
        ("fe80::1%eth0", False),
        ("fd00::1", False),
        ("::ffff:127.0.0.1", False),  # IPv4-mapped loopback
        ("224.0.0.1", False),
        ("not-an-ip", False),
    ],
)
def test_is_public(address, public):
    assert net_guard.is_public(address) is public


@pytest.mark.parametrize("url", ["http://127.0.0.1:9/", "http://169.254.169.254/latest/meta-data/", "http://[::1]/", "http://10.0.0.1/"])
def test_internal_addresses_refused(url):
    with pytest.raises(urllib.error.URLError, match="non-public address"):
        net_guard.opener().open(url, timeout=2)


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.com/x", "data:text/plain,hi"])
def test_other_schemes_refused(url):
    with pytest.raises(urllib.error.URLError, match="unknown url type"):
        net_guard.opener().open(url, timeout=2)


def _fake_dns(monkeypatch, addresses):
    def getaddrinfo(host, port, *a, **k):
        return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in addresses]

    monkeypatch.setattr(net_guard.socket, "getaddrinfo", getaddrinfo)


def test_public_name_resolving_to_internal_address_refused(monkeypatch):
    _fake_dns(monkeypatch, ["10.0.0.5"])
    with pytest.raises(urllib.error.URLError, match="evil.example: it resolves to a non-public address"):
        net_guard.opener().open("http://evil.example/", timeout=2)


def test_any_internal_answer_refuses_the_host(monkeypatch):
    # One public and one private answer: the connection could land on either.
    _fake_dns(monkeypatch, ["93.184.216.34", "127.0.0.1"])
    with pytest.raises(urllib.error.URLError, match="non-public"):
        net_guard.opener().open("https://mixed.example/", timeout=2)


def test_connects_to_the_checked_address(monkeypatch):
    _fake_dns(monkeypatch, ["93.184.216.34"])
    connected = []

    class FakeSocket:
        def __init__(self, *a):
            pass

        def settimeout(self, t):
            pass

        def connect(self, sockaddr):
            connected.append(sockaddr)
            raise OSError("stop here")

        def close(self):
            pass

    monkeypatch.setattr(net_guard.socket, "socket", FakeSocket)
    with pytest.raises(OSError, match="stop here"):
        net_guard._guarded_create_connection(("public.example", 443), 2)
    assert connected == [("93.184.216.34", 443)]


def test_redirect_to_internal_address_refused(monkeypatch):
    # A local server stands in for a public page that redirects to an internal address;
    # only the redirect target (10.0.0.1) is treated as non-public here.
    import http.server
    import threading

    class Redirect(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", "http://10.0.0.1/admin")
            self.end_headers()

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Redirect)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    real = net_guard.is_public
    monkeypatch.setattr(net_guard, "is_public", lambda a: a == "127.0.0.1" or real(a))
    try:
        with pytest.raises(urllib.error.URLError, match=r"non-public address \(10\.0\.0\.1\)"):
            net_guard.opener().open(f"http://127.0.0.1:{server.server_port}/", timeout=5)
    finally:
        server.shutdown()


def test_install_replaces_the_global_opener():
    import urllib.request

    net_guard.install()
    with pytest.raises(urllib.error.URLError, match="non-public address"):
        urllib.request.urlopen("http://127.0.0.1:9/", timeout=2)


def test_public_web_server_installs_the_guard(tmp_path, monkeypatch):
    from paperpush.web.app import Settings, create_app

    calls = []
    monkeypatch.setattr(net_guard, "install", lambda: calls.append(1))
    create_app(Settings(data_dir=tmp_path / "a", network_checks=True))
    assert calls == [1]
    create_app(Settings(data_dir=tmp_path / "b", network_checks=False))
    assert calls == [1]
