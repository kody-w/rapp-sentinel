#!/usr/bin/env python3
"""prove_dashboard_bind.py — the dashboard binds only where configured.

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


bind, host, port = bound_host_for(None)
scenario("default bind address is loopback",
         bind == "127.0.0.1" and host == "127.0.0.1" and port > 0,
         f"bind={bind!r} server_address={(host, port)!r}")

bind, host, port = bound_host_for("127.0.0.1")
scenario("configured SENTINEL_DASH_BIND controls the server bind",
         bind == "127.0.0.1" and host == "127.0.0.1" and port > 0,
         f"bind={bind!r} server_address={(host, port)!r}")

print(f"\n{len(FAILURES)} failing scenario(s)" if FAILURES
      else "\nall scenarios behaved as specified")
sys.exit(1 if FAILURES else 0)
