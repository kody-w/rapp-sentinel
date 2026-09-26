#!/usr/bin/env python3
"""Prove duplicate config keys are visible instead of silently overwritten.

Field incident: Dada Collective's config.json contained evolve_worker.max_piece_bytes
as 10485760 and later 51200. Python json.loads kept the last value, the
azure-reviewed-png preflight failed for weeks, and no check named the duplicate.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent
SCRATCH = ROOT / "state" / "prove-config-integrity"
SCRATCH.mkdir(parents=True, exist_ok=True)
IMPORT_HOME = tempfile.TemporaryDirectory(dir=str(SCRATCH))
os.environ["SENTINEL_HOME"] = IMPORT_HOME.name

import checks as C


class ConfigIntegrityProof(unittest.TestCase):
    def setUp(self):
        self.home_tmp = tempfile.TemporaryDirectory(dir=str(SCRATCH))
        self.addCleanup(self.home_tmp.cleanup)
        self.home = Path(self.home_tmp.name)
        self.patch = mock.patch.object(C, "HOME", self.home)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def write(self, text):
        (self.home / "config.json").write_text(text, encoding="utf-8")

    def test_duplicate_nested_key_fails_and_names_both_values(self):
        self.write('{"evolve_worker":{"max_piece_bytes":10485760,'
                   '"publication_profile":"azure-reviewed-png",'
                   '"max_piece_bytes":51200}}')
        r = C.config_integrity()
        self.assertFalse(r["ok"], r)
        self.assertEqual(C.WARN, r["severity"])
        self.assertIn("evolve_worker.max_piece_bytes appears twice", r["detail"])
        self.assertIn("10485760, then 51200", r["detail"])
        self.assertIn("JSON silently keeps the last", r["detail"])

    def test_invalid_json_fails_but_clean_config_passes(self):
        self.write('{"level":')
        r = C.config_integrity()
        self.assertFalse(r["ok"], r)
        self.assertIn("invalid JSON", r["detail"])
        self.write(json.dumps({"evolve_worker": {"max_piece_bytes": 10485760}}))
        self.assertTrue(C.config_integrity()["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
