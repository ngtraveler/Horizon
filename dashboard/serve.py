#!/usr/bin/env python3
"""Serve the wall dashboard with anti-indexing headers and optional access control.

Standard library only. Three things this does that `python -m http.server` does not:

1. Sends `X-Robots-Tag: noindex, nofollow, noarchive, nosnippet` on every
   response, so crawlers that do reach the port are told not to index it.
   (A request, not a wall: use auth or a private network for real privacy.)
2. Optional HTTP Basic Auth (`--user` + `--password-env`).
3. Optional IP allowlist (`--allow-cidr`, repeatable).

Examples
--------
Local only (default; reach it through an SSH tunnel):
    python3 serve.py

Behind a reverse proxy, with auth:
    DASH_PASSWORD=... python3 serve.py --bind 127.0.0.1 --port 8899 \
        --user radar --password-env DASH_PASSWORD

Directly on a LAN/Tailscale address, restricted by client network:
    python3 serve.py --bind 0.0.0.0 --port 8899 --allow-cidr 100.64.0.0/10
"""

from __future__ import annotations

import argparse
import base64
import hmac
import http.server
import ipaddress
import os
import socketserver
import sys
from functools import partial

NOINDEX = "noindex, nofollow, noarchive, nosnippet"


class Handler(http.server.SimpleHTTPRequestHandler):
    server_version = "MarineRadar"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    # Injected by build_server()
    auth_user = None
    auth_password = None
    allowed_networks = ()
    verbose = False

    # ---- access control ---------------------------------------------------
    def _client_allowed(self) -> bool:
        if not self.allowed_networks:
            return True
        try:
            address = ipaddress.ip_address(self.client_address[0])
        except ValueError:
            return False
        return any(address in network for network in self.allowed_networks)

    def _authorized(self) -> bool:
        if not self.auth_user:
            return True
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header[6:]).decode("utf-8", "replace")
        except Exception:
            return False
        user, _, password = decoded.partition(":")
        return hmac.compare_digest(user, self.auth_user) and hmac.compare_digest(
            password, self.auth_password or ""
        )

    def _reject(self):
        if not self._client_allowed():
            self.send_error(403, "Forbidden")
            return True
        if not self._authorized():
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="Marine Radar", charset="UTF-8"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return True
        return False

    # ---- headers ----------------------------------------------------------
    def end_headers(self):
        self.send_header("X-Robots-Tag", NOINDEX)
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
        super().end_headers()

    # ---- handlers ---------------------------------------------------------
    def do_GET(self):
        if self._reject():
            return
        super().do_GET()

    def do_HEAD(self):
        if self._reject():
            return
        super().do_HEAD()

    def list_directory(self, path):
        # Never expose a directory index; the dashboard only needs named files.
        self.send_error(404, "Not Found")
        return None

    def log_message(self, fmt, *args):
        if self.verbose:
            sys.stderr.write(
                "%s - %s\n" % (self.client_address[0], fmt % args)
            )


class ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def build_server(args):
    Handler.auth_user = args.user
    Handler.auth_password = args.password
    Handler.allowed_networks = tuple(
        ipaddress.ip_network(cidr, strict=False) for cidr in args.allow_cidr
    )
    Handler.verbose = args.verbose
    handler = partial(Handler, directory=args.directory)
    return ThreadingServer((args.bind, args.port), handler)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bind", default="127.0.0.1", help="listen address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8899)
    parser.add_argument("--dir", dest="directory", default=os.path.dirname(os.path.abspath(__file__)))
    parser.add_argument("--user", default=None, help="enable HTTP Basic Auth with this username")
    parser.add_argument(
        "--password-env",
        default="DASH_PASSWORD",
        help="environment variable holding the password (default: DASH_PASSWORD)",
    )
    parser.add_argument(
        "--password",
        default=None,
        help="password on the command line (use --password-env instead; this leaks into ps)",
    )
    parser.add_argument(
        "--allow-cidr",
        action="append",
        default=[],
        help="allow only these client networks, e.g. 100.64.0.0/10 (repeatable)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="log requests")
    args = parser.parse_args(argv)

    if args.user and args.password is None:
        args.password = os.environ.get(args.password_env)
        if not args.password:
            parser.error(
                "user given but ${} is empty; export it or pass --password".format(
                    args.password_env
                )
            )

    if args.bind not in ("127.0.0.1", "::1", "localhost") and not args.user and not args.allow_cidr:
        print(
            "WARNING: listening on {} with no auth and no IP allowlist - "
            "anyone who finds this port can read the dashboard.".format(args.bind),
            file=sys.stderr,
        )

    server = build_server(args)
    protected = "basic-auth" if args.user else ("cidr" if args.allow_cidr else "none")
    print(
        "serving {} on http://{}:{}  (access: {})".format(
            args.directory, args.bind, args.port, protected
        ),
        file=sys.stderr,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped", file=sys.stderr)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
