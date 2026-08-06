"""Reproduction for #50 — do not spend repair budget on outages we cannot repair.

During the 2026-08-06 GitHub incident the sentinel escalated four times in
four hours, the first 32 seconds after GitHub opened the incident, returning
BLOCKED / PARTIAL / BLOCKED / UNKNOWN. Never FIXED. Each spawned
`copilot --allow-all` against the real repositories.
"""
import sys
from datetime import timedelta
import sentinel as S

CASES = []
R_deg, R_now = S.github_degraded, S.now


def case(name, expect, got):
    CASES.append((got == expect, name, expect, got))


def decide(critical, degraded):
    """Mirror the gate in main(): would this escalate?"""
    S.github_degraded = lambda: degraded
    try:
        external = [c for c in critical if c in S.GITHUB_DEPENDENT]
        if len(external) == len(critical) and S.github_degraded():
            return "SKIP"
        return "ESCALATE"
    finally:
        S.github_degraded = R_deg


OUT = ["Actions", "Pages"]

case("all criticals outage-dependent, GitHub down -> skip", "SKIP",
     decide(["rv_validation", "rv_world_merging"], OUT))
case("single dependent critical, GitHub down -> skip", "SKIP",
     decide(["rb_wf_starved"], OUT))
case("dependent criticals but GitHub HEALTHY -> escalate", "ESCALATE",
     decide(["rv_validation", "rv_world_merging"], []))
case("a NON-dependent critical during outage -> escalate", "ESCALATE",
     decide(["rv_validation", "w_brainstem"], OUT))
case("only non-dependent critical, GitHub down -> escalate", "ESCALATE",
     decide(["alert_delivery"], OUT))
case("status page unreachable is NOT an outage -> escalate", "ESCALATE",
     decide(["rv_validation"], []))

# Budget accounting: skipped records must not consume budget.
cfg = {"daily_escalation_budget": 8}
now = S.now()
hist = [{"at": (now - timedelta(hours=1)).isoformat(timespec="seconds"),
         "key": "x", "result": "SKIPPED — external outage", "skipped": True}
        for _ in range(20)]
ok, used = S.within_budget(hist, cfg)
case("20 skipped records do not exhaust budget", (True, 0), (ok, used))

hist2 = hist + [{"at": (now - timedelta(hours=1)).isoformat(timespec="seconds"),
                 "key": "y", "result": "FIXED"} for _ in range(8)]
ok2, used2 = S.within_budget(hist2, cfg)
case("8 real escalations DO exhaust budget", (False, 8), (ok2, used2))

bad = 0
for good, name, expect, got in CASES:
    bad += not good
    print(f"  [{'ok' if good else 'XX'}] {name}\n        expected {expect}  got {got}")
print(f"\n{len(CASES)-bad}/{len(CASES)} scenarios behaved as specified")
sys.exit(1 if bad else 0)
