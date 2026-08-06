"""Reproduction for #48 — attribute an outage to its owner.

Written during a live GitHub incident (2026-08-06 18:10Z, Actions and Pages
both major_outage) that made rappterverse look broken when it was not.
"""
import io, json, sys, urllib.request
import checks

REAL = urllib.request.urlopen


def serve(components):
    urllib.request.urlopen = lambda req, timeout=None: io.BytesIO(
        json.dumps({"components": components}).encode())


def comps(**status):
    base = {"Actions": "operational", "Pages": "operational",
            "API Requests": "operational", "Webhooks": "operational"}
    base.update(status)
    return [{"name": n, "status": s} for n, s in base.items()]


CASES = []


def case(name, want_ok, setup):
    setup()
    try:
        got = checks.github_status_operational()
    finally:
        urllib.request.urlopen = REAL
    CASES.append((got["ok"] is want_ok, name, want_ok, got["ok"], got["detail"][:50]))


case("all four operational", True, lambda: serve(comps()))
case("TODAY: Actions + Pages major_outage", False,
     lambda: serve(comps(Actions="major_outage", Pages="major_outage")))
case("Actions degraded_performance only", False,
     lambda: serve(comps(Actions="degraded_performance")))
case("API Requests partial_outage", False,
     lambda: serve(comps(**{"API Requests": "partial_outage"})))
case("status endpoint unreachable (must NOT read as green)", False,
     lambda: setattr(urllib.request, "urlopen",
                     lambda req, timeout=None: (_ for _ in ()).throw(OSError("dns"))))
case("status page lists none of the watched components", False,
     lambda: serve([{"name": "Codespaces", "status": "operational"}]))
case("unrelated component down is ignored", True,
     lambda: serve(comps() + [{"name": "Copilot", "status": "major_outage"}]))

bad = 0
for good, name, want, got, detail in CASES:
    bad += not good
    print(f"  [{'ok' if good else 'XX'}] {name}\n"
          f"        expected ok={want}  got ok={got}  {detail}")
print(f"\n{len(CASES)-bad}/{len(CASES)} scenarios behaved as specified")
sys.exit(1 if bad else 0)
