#!/usr/bin/env python3
"""prove_dashboard_bind.py — the dashboard binds only where configured.

Default: all interfaces, so texted /share/ report links open on a phone (every
other route already rejects non-loopback clients). SENTINEL_DASH_BIND=127.0.0.1
keeps the server, share links included, on this machine.

Run: python3 prove_dashboard_bind.py
"""

import os
import socketserver
import sys

import serve

FAILURES = []


def scenario(name, cond, observed):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}\n        {observed}")
    if not cond:
        FAILURES.append(name)


def bound_host_for(env_value):
    old = os.environ.get("SENTINEL_DASH_BIND")
    try:
        if env_value is None:
            os.environ.pop("SENTINEL_DASH_BIND", None)
        else:
            os.environ["SENTINEL_DASH_BIND"] = env_value
        bind = serve.bind_address()
        socketserver.TCPServer.allow_reuse_address = True
        with socketserver.TCPServer((bind, 0), serve.Handler) as httpd:
            host, port = httpd.server_address[:2]
            return bind, host, port
    finally:
        if old is None:
            os.environ.pop("SENTINEL_DASH_BIND", None)
        else:
            os.environ["SENTINEL_DASH_BIND"] = old


old = os.environ.pop("SENTINEL_DASH_BIND", None)
try:
    default_bind = serve.bind_address()
finally:
    if old is not None:
        os.environ["SENTINEL_DASH_BIND"] = old
# Checked by value, not by binding: opening an all-interface socket in a proof
# would itself expose this machine (and trip the macOS firewall prompt).
scenario("default bind keeps phone share links reachable (all interfaces)",
         default_bind == "0.0.0.0",
         f"bind={default_bind!r}")

bind, host, port = bound_host_for("127.0.0.1")
scenario("SENTINEL_DASH_BIND=127.0.0.1 binds loopback only",
         bind == "127.0.0.1" and host == "127.0.0.1" and port > 0,
         f"bind={bind!r} server_address={(host, port)!r}")

print(f"\n{len(FAILURES)} failing scenario(s)" if FAILURES
      else "\nall scenarios behaved as specified")
sys.exit(1 if FAILURES else 0)
