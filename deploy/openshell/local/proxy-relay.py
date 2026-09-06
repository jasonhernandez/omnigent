#!/usr/bin/env python3
"""Loopback TCP relay that reaches the Omnigent server through OpenShell's proxy.

OpenShell forces sandbox egress through its policy proxy and refuses direct TCP.
The host's server tunnel is a WebSocket opened by websockets<15, which has no
proxy support (the cap is deliberate — see pyproject), so it cannot dial the
server itself. This relay listens on loopback (exempt from both the proxy and the
egress policy) and re-sends each request to the proxy in absolute-URI form, which
the proxy forwards — WebSocket upgrade included (it answers 101). CONNECT is not
used: the proxy serves plaintext endpoints at L7 and refuses CONNECT with 403.
"""

from __future__ import annotations

import contextlib
import os
import socket
import socketserver
import sys
import threading
from urllib.parse import urlparse

LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = int(os.environ.get("OMNIGENT_RELAY_PORT", "6868"))
TARGET_HOST = os.environ.get("OMNIGENT_RELAY_TARGET_HOST", "host.openshell.internal")
TARGET_PORT = int(os.environ.get("OMNIGENT_RELAY_TARGET_PORT", str(LISTEN_PORT)))


def _proxy_address() -> tuple[str, int]:
    """Resolve the sandbox's CONNECT proxy from the standard proxy env vars."""
    for var in ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY", "ALL_PROXY"):
        raw = os.environ.get(var)
        if raw:
            parsed = urlparse(raw if "://" in raw else f"http://{raw}")
            if parsed.hostname:
                return parsed.hostname, parsed.port or 3128
    raise SystemExit("no proxy env var set; nothing to relay through")


def _pipe(src: socket.socket, dst: socket.socket) -> None:
    try:
        while chunk := src.recv(65536):
            dst.sendall(chunk)
    except OSError:
        pass
    finally:
        for sock in (src, dst):
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)


class _Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        proxy_host, proxy_port = _proxy_address()
        upstream = socket.create_connection((proxy_host, proxy_port), timeout=30)
        try:
            head = b""
            while b"\r\n" not in head:
                block = self.request.recv(4096)
                if not block:
                    return
                head += block
            line, rest = head.split(b"\r\n", 1)
            parts = line.split(b" ")
            if len(parts) != 3 or not parts[1].startswith(b"/"):
                raise OSError(f"unexpected request line: {line!r}")
            method, path, version = parts
            # Origin-form path -> absolute URI: what an HTTP proxy requires to
            # forward the request (and the upgrade riding on it) upstream.
            absolute = f"http://{TARGET_HOST}:{TARGET_PORT}".encode() + path
            upstream.sendall(b" ".join((method, absolute, version)) + b"\r\n" + rest)
            upstream.settimeout(None)
            thread = threading.Thread(target=_pipe, args=(self.request, upstream), daemon=True)
            thread.start()
            _pipe(upstream, self.request)
            thread.join()
        except OSError as exc:
            print(f"relay: {exc}", file=sys.stderr, flush=True)
        finally:
            upstream.close()


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


if __name__ == "__main__":
    with _Server((LISTEN_HOST, LISTEN_PORT), _Handler) as server:
        print(
            f"relay listening on {LISTEN_HOST}:{LISTEN_PORT} -> "
            f"{TARGET_HOST}:{TARGET_PORT} via {':'.join(map(str, _proxy_address()))}",
            flush=True,
        )
        server.serve_forever()
