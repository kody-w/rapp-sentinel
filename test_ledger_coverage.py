"""The coverage table in MUTATION-LEDGER.md is a guard, not a memorial.

Every id in required_checks.json must have a row — proven, lands-with-PR,
or an explicit UNPROVEN entry. A new check landing without even an UNPROVEN
row fails the suite, so the ledger cannot silently fall behind the manifest.
The table may hold ids the manifest no longer lists: a retired check keeps
its history, and history is never a failure.
"""
import json
import re
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LEDGER = ROOT / "MUTATION-LEDGER.md"
MANIFEST = ROOT / "required_checks.json"

COVERAGE_HEADING = "## Coverage"
ROW = re.compile(r"^\|\s*`([A-Za-z0-9_]+)`\s*\|")


def coverage_ids(text):
    """Ids in the first column of the coverage table.

    Only rows after the coverage heading count — the 19-verdict table
    earlier in the file is the historical record, not the living guard.
    """
    _, sep, tail = text.partition(COVERAGE_HEADING)
    if not sep:
        return set()
    ids = set()
    for line in tail.splitlines():
        m = ROW.match(line.strip())
        if m and m.group(1) != "id":
            ids.add(m.group(1))
    return ids


class LedgerCoverageTests(unittest.TestCase):
    def test_every_required_id_has_a_coverage_row(self):
        required = set(
            json.loads(MANIFEST.read_text(encoding="utf-8"))["required"])
        self.assertTrue(required, "manifest lists no required checks")
        covered = coverage_ids(LEDGER.read_text(encoding="utf-8"))
        missing = sorted(required - covered)
        self.assertEqual(
            [], missing,
            "required check(s) without a MUTATION-LEDGER.md coverage row "
            "(add one, even if it just says UNPROVEN): " + ", ".join(missing))

    def test_table_may_hold_retired_ids_the_manifest_lacks(self):
        required = set(
            json.loads(MANIFEST.read_text(encoding="utf-8"))["required"])
        text = LEDGER.read_text(encoding="utf-8")
        retired = "| `zz_retired_check_kept_for_history` | ledger row | proven |\n"
        covered = coverage_ids(text + retired)
        # An id beyond the manifest is parsed, and never fails the comparison.
        self.assertIn("zz_retired_check_kept_for_history", covered)
        self.assertNotIn("zz_retired_check_kept_for_history", required)
        self.assertEqual(sorted(required - coverage_ids(text)),
                         sorted(required - covered))

    def test_deleting_a_row_from_a_scratch_copy_is_detected(self):
        """The guard fires: this is the mutation that proves the check."""
        required = json.loads(
            MANIFEST.read_text(encoding="utf-8"))["required"]
        victim = required[0]
        text = LEDGER.read_text(encoding="utf-8")
        head, sep, tail = text.partition(COVERAGE_HEADING)
        kept = [line for line in tail.splitlines()
                if not (ROW.match(line.strip())
                        and ROW.match(line.strip()).group(1) == victim)]
        mutated = head + sep + "\n".join(kept)
        self.assertNotEqual(text, mutated,
                            f"harness broke nothing: no row for {victim!r}")
        with tempfile.TemporaryDirectory() as raw:
            scratch = Path(raw) / "MUTATION-LEDGER.md"
            scratch.write_text(mutated, encoding="utf-8")
            covered = coverage_ids(scratch.read_text(encoding="utf-8"))
        self.assertNotIn(victim, covered)
        self.assertIn(victim, coverage_ids(text))  # control: intact file passes

    def test_harness_error_class_no_heading_means_no_coverage(self):
        """A file without the coverage heading proves nothing, and must
        read as covering nothing — not as vacuously complete."""
        self.assertEqual(set(), coverage_ids("| `alert_delivery` | x | y |"))


if __name__ == "__main__":
    unittest.main()
