#!/usr/bin/env python3
"""Prove GitHub CLI blindness and configured identity are visible.

Field incident: Dada Collective's checks ran as active gh account rappter1 whose
GraphQL limit was 0, so rv_pr_queue and rb_rollup_coverage were blind 3,101
times each. A second account in the same keyring had quota. The fake gh here
never talks to GitHub and records only whether GH_TOKEN was set, never its value.
"""

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent
SCRATCH = ROOT / "state" / "prove-gh-identity"
SCRATCH.mkdir(parents=True, exist_ok=True)
IMPORT_HOME = tempfile.TemporaryDirectory(dir=str(SCRATCH))
os.environ["SENTINEL_HOME"] = IMPORT_HOME.name

import checks as C
import paths

SECRET = "SECRET_TOKEN_DO_NOT_PRINT"


class GhIdentityProof(unittest.TestCase):
    def setUp(self):
        self.root_tmp = tempfile.TemporaryDirectory(dir=str(SCRATCH))
        self.addCleanup(self.root_tmp.cleanup)
        self.root = Path(self.root_tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.fakebin = self.root / "bin"
        self.fakebin.mkdir()
        self.log = self.root / "fake-gh.log"
        self.emptybin = self.root / "empty-bin"
        self.emptybin.mkdir()
        self.write_fake_gh()
        self.env = mock.patch.dict(os.environ, {
            "PATH": str(self.fakebin),
            "FAKE_GH_MODE": "healthy",
            "FAKE_GH_LOG": str(self.log),
        }, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.home_patch = mock.patch.object(C, "HOME", self.home)
        self.home_patch.start()
        self.addCleanup(self.home_patch.stop)
        self.paths_home_patch = mock.patch.object(paths, "HOME", self.home)
        self.paths_home_patch.start()
        self.addCleanup(self.paths_home_patch.stop)
        self.reset_gh_cache()

    def reset_gh_cache(self):
        C._GH_LAST_FAILURE = ""
        C._GH_TOKEN_CACHE.update({"user": None, "token": None,
                                  "cause": None, "loaded": False})

    def write_config(self, data):
        (self.home / "config.json").write_text(json.dumps(data), encoding="utf-8")
        self.reset_gh_cache()

    def write_fake_gh(self):
        script = r'''#!/usr/bin/python3
import json, os, sys
args = sys.argv[1:]
log = os.environ.get("FAKE_GH_LOG")
if log:
    with open(log, "a", encoding="utf-8") as f:
        f.write("GH_TOKEN=" + ("set" if os.environ.get("GH_TOKEN") else "unset") + "\n")
mode = os.environ.get("FAKE_GH_MODE", "healthy")
if args[:4] == ["auth", "token", "--user", "kody-w"]:
    print("SECRET_TOKEN_DO_NOT_PRINT")
    sys.exit(0)
if args[:2] == ["auth", "token"]:
    print("unknown user", file=sys.stderr)
    sys.exit(1)
if args[:2] == ["api", "rate_limit"]:
    limit = 0 if mode == "quota0" else 5000
    remaining = 0 if mode == "quota0" else 4999
    print(json.dumps({"resources":{"graphql":{"limit":limit,"remaining":remaining}}}))
    sys.exit(0)
if args[:2] == ["api", "user"]:
    print(json.dumps({"login":"kody-w" if os.environ.get("GH_TOKEN") else "rappter1"}))
    sys.exit(0)
if args[:2] == ["pr", "list"]:
    if mode == "exit2":
        print("GraphQL rate limit exceeded", file=sys.stderr)
        sys.exit(2)
    print("[]")
    sys.exit(0)
if args[:2] == ["api", "graphql"]:
    print(json.dumps({"data":{"repository":{"discussions":{"totalCount":1}}}}))
    sys.exit(0)
print("unexpected", args, file=sys.stderr)
sys.exit(3)
'''
        path = self.fakebin / "gh"
        path.write_text(script, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def test_quota_zero_fails_with_remedy(self):
        os.environ["FAKE_GH_MODE"] = "quota0"
        r = C.gh_identity()
        self.assertFalse(r["ok"], r)
        self.assertIn("gh identity rappter1 has a GraphQL quota of 0", r["detail"])
        self.assertIn("Set gh_user in config.json", r["detail"])

    def test_healthy_identity_is_ok(self):
        r = C.gh_identity()
        self.assertTrue(r["ok"], r)
        self.assertIn("rappter1", r["detail"])
        self.assertIn("4999/5000", r["detail"])

    def test_configured_gh_user_passes_token_without_printing_it(self):
        self.write_config({"gh_user": "kody-w"})
        r = C.gh_identity()
        self.assertTrue(r["ok"], r)
        self.assertIn("kody-w", r["detail"])
        transcript = self.log.read_text(encoding="utf-8")
        self.assertIn("GH_TOKEN=set", transcript)
        self.assertNotIn(SECRET, transcript)
        self.assertNotIn(SECRET, json.dumps(r))

    def test_missing_gh_records_cause_for_callers(self):
        with mock.patch.dict(os.environ, {"PATH": str(self.emptybin)}, clear=False):
            self.reset_gh_cache()
            r = C.queue_draining()
        self.assertFalse(r["ok"], r)
        self.assertIn("cannot read the PR queue", r["detail"])
        self.assertIn("gh binary not found", r["detail"])
        self.assertEqual("gh binary not found", C.gh_failure_cause())


if __name__ == "__main__":
    unittest.main(verbosity=2)
