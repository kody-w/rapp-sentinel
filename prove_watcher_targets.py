#!/usr/bin/env python3
"""prove_watcher_targets.py — probe_watchers reads its estate targets from
config.json, a disabled probe is a DECLARATION rather than a silent green,
and — the molt control — a config without the `watchers` key behaves
identically to the hardcoded values it replaced (issue #1 ask 2).

The trial neighborhood (rapp-vision-neighborhood) hit this exactly:
checks.py claims to be the only file most people edit, while
health.py::probe_watchers hardcoded a brainstem POST and launchd labels
from one estate — two of three probes permanently red on any other machine,
which trains the reader to ignore the surface (the nineteen-days failure
inverted). Targets now merge from config.json's `watchers` block through
ONE read path (_watcher_config), including the spin scan's candidate
labels.

Everything external is stubbed: health._run gets canned launchctl/lsof
transcripts, urllib.request.urlopen is captured (proving which URL was
ACTUALLY probed, not which one the config claims), and the neighborhood
ledger check is a stub module. Deterministic on purpose, so the molt
control can compare full result payloads.

Run: python3 prove_watcher_targets.py   (exit 0 only on all-behaved)
"""

import json
import sys
import tempfile
import types
import urllib.request
from pathlib import Path

import checks as C
import health as H

FAILURES = []


def scenario(name, cond, observed):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}\n        {observed}")
    if not cond:
        FAILURES.append(name)


REAL_HOME = H.HOME
REAL_RUN = H._run
REAL_URLOPEN = urllib.request.urlopen
REAL_NEIGHBORHOOD = sys.modules.get("neighborhood")

# Canned transcripts: nothing LISTENING, readable-but-empty launchd domains.
GUI_LIST = "PID\tStatus\tLabel\n-\t0\tcom.something.else"
SYS_PRINT = ("system information:\n\tservices = {\n"
             "\t\t       0      - \tcom.apple.rpmuxd\n\t}\n")


def canned_run(cmd, timeout=15):
    if cmd[0] == "lsof":
        return ""
    if cmd[:2] == ["launchctl", "list"]:
        return GUI_LIST
    if cmd[:2] == ["launchctl", "print"]:
        return SYS_PRINT if cmd[2] == "system" else ""
    return ""


class _FakeResp:
    def __init__(self, body):
        self._body = body
        self.status = 200

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


URL_CALLS = []


def capturing_urlopen(req, timeout=None):
    URL_CALLS.append((req.full_url, timeout))
    return _FakeResp(json.dumps({"response": "ok"}).encode())


def home_with(config=None):
    """A scratch HOME carrying the committed manifest and, optionally, a
    config.json. No state/ dir, so w_sentinel_fresh reads 'first run'."""
    d = Path(tempfile.mkdtemp(prefix="prove-watchers-"))
    (d / "required_checks.json").write_text(
        (REAL_HOME / "required_checks.json").read_text(encoding="utf-8"),
        encoding="utf-8")
    if config is not None:
        (d / "config.json").write_text(json.dumps(config), encoding="utf-8")
    H.HOME = d
    return d


fake_nb = types.ModuleType("neighborhood")
fake_nb.check_external_ledger = lambda: {"status": "ok",
                                         "detail": "stub ledger, chain intact"}
sys.modules["neighborhood"] = fake_nb
H._run = canned_run
urllib.request.urlopen = capturing_urlopen

try:
    # (1) A partial override merges over the defaults — one key changed,
    # every other key keeps today's value, other watchers untouched.
    home_with({"watchers": {"brainstem": {"url": "http://localhost:9999/chat"}}})
    cfg = H._watcher_config()
    scenario("(1) partial override: url changes, enabled/timeout keep the "
             "defaults, openrappter untouched",
             cfg["brainstem"] == {"enabled": True,
                                  "url": "http://localhost:9999/chat",
                                  "timeout": 30}
             and cfg["openrappter"] == H.WATCHER_DEFAULTS["openrappter"],
             json.dumps(cfg["brainstem"]))

    # (2) The override changes the URL ACTUALLY probed, not just the merge.
    URL_CALLS[:] = []
    results, disabled = H.probe_watchers()
    ids = [r["id"] for r in results]
    scenario("(2) the configured URL is the one urlopen was handed, with the "
             "default timeout",
             URL_CALLS == [("http://localhost:9999/chat", 30)]
             and "w_brainstem" in ids and disabled == [],
             f"urlopen saw {URL_CALLS}; disabled={disabled}")

    # (3) enabled:false — the probe does not run; the id rides back as a
    # declared omission, never a phantom green or a phantom deletion.
    home_with({"watchers": {"brainstem": {"enabled": False}}})
    URL_CALLS[:] = []
    results, disabled = H.probe_watchers()
    ids = [r["id"] for r in results]
    scenario("(3) enabled:false -> no POST is made, w_brainstem absent from "
             "results, id returned as disabled",
             URL_CALLS == [] and "w_brainstem" not in ids
             and disabled == ["w_brainstem"],
             f"urlopen saw {URL_CALLS}; ids={ids}; disabled={disabled}")

    required = set(json.loads(
        (REAL_HOME / "required_checks.json").read_text(encoding="utf-8"))
        ["required"])
    stubs = [{"id": cid, "ok": True, "severity": C.WARN, "detail": "",
              "produced_by": "stub"}
             for cid in required - {"w_brainstem", "w_checks_complete"}]

    v = H.check_completeness(stubs, disabled=("w_brainstem",))
    scenario("(4) w_checks_complete PASSES and DECLARES the omission in its "
             "detail on every verdict",
             v["ok"] and "disabled by config" in v["detail"]
             and "w_brainstem" in v["detail"],
             v["detail"])

    v = H.check_completeness(stubs)
    scenario("(5) BREAK CONTROL: same results without the declaration is "
             "still the critical missing-check alarm",
             (not v["ok"]) and v["severity"] == C.CRITICAL
             and "w_brainstem" in v["detail"],
             f"severity={v['severity']} {v['detail']}")

    v = H.check_completeness(stubs, disabled=("w_brainstem", "w_not_a_check"))
    scenario("(6) a disabled id the manifest does not know is flagged as a "
             "config typo, not quietly ignored",
             v["ok"] and "config typo" in v["detail"]
             and "w_not_a_check" in v["detail"],
             v["detail"])

    # (7) Disabling the port probe does NOT disable the spin scan — separate
    # declarations, and a spinning job matters even with the port dark.
    home_with({"watchers": {"openrappter": {"enabled": False}}})
    results, disabled = H.probe_watchers()
    ids = [r["id"] for r in results]
    scenario("(7) openrappter disabled: port probe off, spin scan still runs",
             "w_openrappter" not in ids and "w_openrappter_spin" in ids
             and disabled == ["w_openrappter"],
             f"ids={ids}; disabled={disabled}")

    # (8) ONE read path: the spin scan's candidate labels come from the same
    # merged watchers.openrappter.labels key the port probe is handed.
    home_with({"watchers": {"openrappter": {"labels": ["com.custom.twin"]}}})
    labels, readable = H._openrappter_candidate_labels()
    cfg = H._watcher_config()
    scenario("(8) a configured label reaches BOTH the spin scan (unioned with "
             "the known set) and the merged probe config",
             readable and "com.custom.twin" in labels
             and set(H._OPENRAPPTER_LABELS) <= labels
             and cfg["openrappter"]["labels"] == ["com.custom.twin"],
             f"candidates={sorted(labels)}")

    # (9) THE MOLT CONTROL: no config, empty config, and empty watchers block
    # all merge to today's exact literals and produce identical probe results.
    payloads = []
    for label, config in (("absent", None), ("empty", {}),
                          ("empty watchers", {"watchers": {}})):
        home_with(config)
        URL_CALLS[:] = []
        results, disabled = H.probe_watchers()
        payloads.append((label, json.dumps(results, sort_keys=True),
                         list(disabled), URL_CALLS[:]))
    same = (payloads[0][1] == payloads[1][1] == payloads[2][1]
            and all(p[2] == [] for p in payloads)
            and all(p[3] == [("http://localhost:7071/chat", 30)]
                    for p in payloads))
    scenario("(9) molt control: config absent / {} / empty watchers -> "
             "identical results, nothing disabled, the default URL probed",
             same and H._watcher_config() == H.WATCHER_DEFAULTS,
             f"probed={payloads[0][3]}; payloads identical={same}")

    home_with(None)
    scenario("(10) merged defaults ARE today's literals, key for key",
             H._watcher_config() == {
                 "brainstem": {"enabled": True,
                               "url": "http://localhost:7071/chat",
                               "timeout": 30},
                 "openrappter": {"enabled": True, "port": 18790,
                                 "labels": ["com.openrappter.rapptertwo",
                                            "com.openrappter.gateway",
                                            "com.openrappter.daemon"]}},
             json.dumps(H._watcher_config(), sort_keys=True)[:120])
finally:
    H.HOME = REAL_HOME
    H._run = REAL_RUN
    urllib.request.urlopen = REAL_URLOPEN
    if REAL_NEIGHBORHOOD is not None:
        sys.modules["neighborhood"] = REAL_NEIGHBORHOOD
    else:
        sys.modules.pop("neighborhood", None)

print(f"\n{len(FAILURES)} failing scenario(s)" if FAILURES
      else "\nall scenarios behaved as specified")
sys.exit(1 if FAILURES else 0)
