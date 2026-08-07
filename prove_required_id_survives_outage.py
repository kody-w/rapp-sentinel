"""Reproduction for the false critical — a required id must survive its outage.

`sites_up` and `channel_serving` probe several URLs and are registered in
required_checks.json under the id they return when everything is up: `sites`
and `channel`. Both returned the PER-URL result on the failure path, so the
required id was missing exactly when the platform was broken:

    ch_home  [warn]     HTTP 503 - https://kody-w.github.io/rappvision-field-notes/
    w_checks_complete   [critical] 1 required check(s) did not run: channel

Nothing had happened to the registry. One GitHub Pages URL blipped 503 for a
few minutes, and a warn-level external outage was reported as the one alarm
reserved for a check having been DELETED. The louder signal was the false one,
and the two conditions are not remotely the same repair.

So each scenario asserts three things -- the id, the ok, and the severity --
because getting the id back would be worthless if the outage stopped being
reported, and reporting it as critical would just move the lie. Every outage
case is paired with a healthy control: the point is not to make these checks
quieter, it is to make "the channel is down" distinct from "the check is gone".
"""
import json
import sys

import checks
import health

REAL = checks.http_status
CASES = []


def status_map(m):
    """Serve `m` by URL suffix; anything unlisted is 200."""
    def fake(url):
        for suffix, code in m.items():
            if url.rstrip("/").endswith(suffix):
                return code
        return 200
    return fake


def case(name, fn, want_id, want_ok, want_sev, urls):
    checks.http_status = status_map(urls)
    try:
        got = fn()
    except Exception as e:
        got = {"id": f"raised {type(e).__name__}", "ok": None, "severity": "?",
               "detail": str(e)}
    finally:
        checks.http_status = REAL
    good = (got["id"] == want_id and got["ok"] is want_ok
            and got["severity"] == want_sev)
    CASES.append((good, name, f"{want_id}/{want_ok}/{want_sev}",
                  f"{got['id']}/{got['ok']}/{got['severity']}",
                  got.get("detail", "")[:60]))
    return got


# ── channel: every failing URL still reports under `channel` ────────────────
case("channel: ch_home 503 (the outage that fired the false critical)",
     checks.channel_serving, "channel", False, checks.WARN,
     {"field-notes": 503})
case("channel: ch_json 404", checks.channel_serving, "channel", False,
     checks.WARN, {"channel.json": 404})
case("channel: ch_feed unreachable", checks.channel_serving, "channel", False,
     checks.WARN, {"feed.xml": 0})
case("channel: CONTROL all three serving", checks.channel_serving,
     "channel", True, checks.WARN, {})

# ── sites: same shape, same latent defect ──────────────────────────────────
case("sites: rv_site 503", checks.sites_up, "sites", False, checks.WARN,
     {"rappterverse": 503})
case("sites: rb_site 500", checks.sites_up, "sites", False, checks.WARN,
     {"rappterbook": 500})
case("sites: CONTROL both serving", checks.sites_up, "sites", True,
     checks.WARN, {})

# ── the detail must still name WHICH url failed ────────────────────────────
got = case("channel: outage names the failing sub-probe and its url",
           checks.channel_serving, "channel", False, checks.WARN,
           {"field-notes": 503})
named = "ch_home" in got["detail"] and "field-notes" in got["detail"]
CASES.append((named, "channel: detail keeps 'ch_home' and the url",
              "detail names sub-probe", "yes" if named else "NO",
              got["detail"][:60]))


# ── end to end: a URL outage must not read as a missing check ──────────────
def completeness_after(urls):
    """Run the two fan-out checks for real, then ask check_completeness what
    it makes of the ids they produced. Other checks emit their required id on
    every path, so they are granted."""
    checks.http_status = status_map(urls)
    try:
        results = []
        for fn in (checks.sites_up, checks.channel_serving):
            r = fn()
            r.setdefault("produced_by", fn.__name__)
            results.append(r)
    finally:
        checks.http_status = REAL
    granted = set(json.loads((health.HOME / "required_checks.json")
                             .read_text(encoding="utf-8"))["required"])
    granted -= {"sites", "channel", "w_checks_complete"}
    results += [{"id": c, "ok": True, "severity": checks.WARN, "detail": "",
                 "produced_by": "stub"} for c in granted]
    return health.check_completeness(results)


for label, urls in (("channel down", {"field-notes": 503}),
                    ("a site down", {"rappterverse": 503}),
                    ("both down", {"field-notes": 503, "rappterverse": 503}),
                    ("CONTROL nothing down", {})):
    v = completeness_after(urls)
    CASES.append((v["ok"] is True,
                  f"w_checks_complete: stays green while {label}",
                  "ok=True", f"ok={v['ok']}", v["detail"][:60]))

# The control that proves this proof can fail: a genuinely absent check must
# still be caught, or the fix would have been "stop looking".
v = health.check_completeness([{"id": "sites", "ok": True, "severity": checks.WARN,
                                "detail": "", "produced_by": "sites_up"}])
CASES.append((v["ok"] is False and v["severity"] == checks.CRITICAL,
              "w_checks_complete: CONTROL a truly missing check still goes critical",
              "ok=False/critical", f"ok={v['ok']}/{v['severity']}", v["detail"][:60]))

bad = 0
for good, name, want, got_s, detail in CASES:
    bad += not good
    print(f"  [{'ok' if good else 'XX'}] {name}\n"
          f"        expected {want}  got {got_s}  {detail}")
print(f"\n{len(CASES) - bad}/{len(CASES)} scenarios behaved as specified")
sys.exit(1 if bad else 0)
