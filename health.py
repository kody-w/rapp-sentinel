#!/usr/bin/env python3
"""health.py — run every registered check, plus the watcher self-checks.

Costs nothing but API calls: no model is invoked here. The sentinel only
escalates when this reports something actually broken, which is what makes
running every 15 minutes forever affordable.

Domain checks live in checks.py — that is the file you edit. This file is the
runner and should rarely need changing.

Exit code is always 0; the verdict is the payload, not the status.
"""

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import checks as C

HOME = Path(__file__).resolve().parent


def probe_watchers():
    """The watchers watching the watchmen.

    A watcher that dies quietly is worse than no watcher, because everything
    downstream keeps reporting green off a record that stopped moving. So each
    peer is checked here, and the sentinel checks its own freshness too — a
    stalled loop cannot notice that it stalled, so it leaves a timestamp behind
    for the next run, and for the other two, to judge.
    """
    out = []
    out.append(C.url_check("w_brainstem", "http://localhost:7071/", critical=False))

    try:
        r = subprocess.run(["launchctl", "list"], capture_output=True,
                           text=True, timeout=10)
        loaded = "com.openrappter.daemon" in (r.stdout or "")
    except Exception:
        loaded = False
    out.append(C.ok("w_openrappter", "daemon loaded") if loaded
               else C.fail("w_openrappter", "launchd daemon not loaded", critical=False))

    beat = HOME / "state" / "last_run.json"
    age_m = None
    if beat.exists():
        try:
            prev = json.loads(beat.read_text(encoding="utf-8"))
            age_m = (datetime.now(timezone.utc) - datetime.fromisoformat(
                prev["at"].replace("Z", "+00:00"))).total_seconds() / 60
        except Exception:
            age_m = None
    out.append(C.ok("w_sentinel_fresh", "first run" if age_m is None
                    else f"last tick {age_m:.0f}m ago")
               if age_m is None or age_m < 90
               else C.fail("w_sentinel_fresh", f"last tick {age_m:.0f}m ago", critical=False))
    return out


def main():
    results = []
    for fn in C.all_checks():
        try:
            r = fn()
            results.append(r if isinstance(r, dict) else
                           C.fail(fn.__name__, "check returned a non-result", critical=False))
        except Exception as e:
            # a check that throws is a broken check, not a broken platform
            results.append(C.fail(fn.__name__,
                                  f"check raised {type(e).__name__}: {e}", critical=False))
    results += probe_watchers()

    failed = [c for c in results if not c["ok"]]
    crit = [c for c in failed if c["severity"] == C.CRITICAL]

    print(json.dumps({
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": "critical" if crit else ("degraded" if failed else "healthy"),
        "checks": results,
        "failed": [c["id"] for c in failed],
        "critical": [c["id"] for c in crit],
        "summary": "; ".join(f"{c['id']}: {c['detail']}" for c in failed)
                   or "all checks passing",
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
