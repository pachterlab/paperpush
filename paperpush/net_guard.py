"""Keep a public server from fetching internal addresses (SSRF guard).

With network checks on, ``validate`` requests URLs taken from the visitor's
files: link reachability, DOI and registry lookups, linked anonymous
repositories, Zenodo/Figshare/OSF records, and project pages. On a public
deployment (paperpush.onrender.com) a crafted link could point that request at
the server's own network instead: ``http://127.0.0.1:...``, a cloud metadata
endpoint (169.254.169.254), or a private service next to it.

:func:`install` replaces urllib's global opener with one whose HTTP and HTTPS
connections resolve the host themselves, refuse the request unless *every*
address is public, and then connect to the address that was checked, so a DNS
answer cannot change between the check and the connection. Redirects go through
the same opener, so a public URL that redirects to an internal one is refused
too. Every fetch in paperpush goes through ``urllib.request.urlopen``, so this
covers them all; a refused request fails like an unreachable host, which the
checks already treat as "could not check".

Only the public web server installs it. The CLI and a ``paperpush web --web``
session run on the author's own machine, where an internal address is the
author's own business.
"""

from __future__ import annotations

import http.client
import ipaddress
import socket
import urllib.request

_installed = False


class BlockedAddress(OSError):
    """The host resolves to an address a public server must not contact."""


def is_public(address: str) -> bool:
    """Whether ``address`` (an IP literal) is a public, routable unicast address."""
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def _guarded_create_connection(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None, *args, **kwargs):
    """``socket.create_connection`` that connects only to public addresses, and only to the ones it checked."""
    host, port = address[0], address[1]
    infos = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    if not infos:
        raise BlockedAddress(f"{host} did not resolve")
    blocked = [info[4][0] for info in infos if not is_public(info[4][0])]
    if blocked:
        raise BlockedAddress(f"refusing to connect to {host}: it resolves to a non-public address ({blocked[0]})")
    error: OSError | None = None
    for family, socktype, proto, _, sockaddr in infos:
        sock = socket.socket(family, socktype, proto)
        try:
            if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            sock.connect(sockaddr)
            return sock
        except OSError as exc:
            error = exc
            sock.close()
    raise error or BlockedAddress(f"could not connect to {host}")


class _GuardedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = _guarded_create_connection


class _GuardedHTTPSConnection(http.client.HTTPSConnection):
    # HTTPSConnection.connect calls HTTPConnection.connect (which uses
    # _create_connection) and then wraps the socket with TLS for self.host, so
    # certificate checks still use the hostname.
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = _guarded_create_connection


class _GuardedHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_GuardedHTTPConnection, req)


class _GuardedHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_GuardedHTTPSConnection, req, context=self._context)


def opener() -> urllib.request.OpenerDirector:
    """An opener for http(s) only, whose connections refuse non-public addresses.

    Built by hand rather than with ``build_opener``, which would add the
    ``file:``, ``ftp:``, and ``data:`` handlers a crafted link could also name;
    here those fail as unknown URL types.
    """
    director = urllib.request.OpenerDirector()
    for handler in (
        urllib.request.ProxyHandler(),
        urllib.request.UnknownHandler(),
        _GuardedHTTPHandler(),
        _GuardedHTTPSHandler(),
        urllib.request.HTTPDefaultErrorHandler(),
        urllib.request.HTTPRedirectHandler(),
        urllib.request.HTTPErrorProcessor(),
    ):
        director.add_handler(handler)
    return director


def install() -> None:
    """Make every ``urllib.request.urlopen`` in this process use :func:`opener`. Idempotent."""
    global _installed
    if not _installed:
        urllib.request.install_opener(opener())
        _installed = True

