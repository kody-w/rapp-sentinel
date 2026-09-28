#!/usr/bin/env python3
"""Linear anchor checking must reproduce every old digest and tamper verdict."""
import copy
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent
SCRATCH = ROOT / "state" / "prove-anchor-performance"
SCRATCH.mkdir(parents=True, exist_ok=True)
IMPORT_HOME = tempfile.TemporaryDirectory(dir=str(SCRATCH))
os.environ["SENTINEL_HOME"] = IMPORT_HOME.name

import neighborhood as NB
import rapp


def legacy_check():
    """Pre-optimization algorithm: the compatibility oracle, not a cache."""
    if not NB.ANCHORS.exists():
        return {}
    seen = {}
    for line in NB.ANCHORS.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except Exception:
            continue
        for slug, head in record.get("heads", {}).items():
            previous = seen.get(slug)
            if previous is None or head["seq"] > previous["seq"]:
                seen[slug] = head
    out = {}
    for slug, high in seen.items():
        chain = NB.read_chain(slug)
        current = chain[-1]["seq"] if chain else -1
        hashes = {frame["frame_hash"] for frame in chain}
        revised_at = None
        for anchor in NB.anchors_for(slug):
            digest = anchor.get("chain_digest")
            if not digest or anchor["seq"] > current:
                continue
            prefix = chain[:anchor["seq"] + 1]
            if len(prefix) == anchor["seq"] + 1 and NB.chain_digest(prefix) != digest:
                revised_at = (anchor["seq"] if revised_at is None
                              else min(revised_at, anchor["seq"]))
        out[slug] = {
            "witnessed_seq": high["seq"], "current_seq": current,
            "truncated": current < high["seq"],
            "witnessed_head_present": high["frame_hash"] in hashes or current > high["seq"],
            "revised": revised_at is not None, "revised_before_seq": revised_at,
        }
    return out


class AnchorPerformanceProof(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=str(SCRATCH))
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        for name, value in (
                ("NBHD", self.home), ("IDENTITY", self.home / "neighbors.json"),
                ("ANCHORS", self.home / "anchors.jsonl"),
                ("EXTERNAL_LEDGER", self.home / "outside" / "ledger.json"),
                ("NEIGHBORS", {"copilot": "proof", "other": "second proof"})):
            patcher = mock.patch.object(NB, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.ids = NB.identities()
        self.chain = self.frames(32)
        self.write_chain(self.chain)
        self.anchors = self.anchor_rows(self.chain)
        self.write_anchors()

    def frames(self, count, slug="copilot"):
        chain = []
        start = datetime(2026, 9, 28, tzinfo=timezone.utc)
        for seq in range(count):
            stamp = (start + timedelta(seconds=seq)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
            chain.append(rapp.build_frame(
                "sentinel.tick", self.ids[slug], seq, stamp,
                {"value": "AAAA"}, chain[-1]["payload_hash"] if chain else None))
        return chain

    def anchor_rows(self, chain, slug="copilot"):
        return [{"utc": frame["utc"], "heads": {slug: {
            "seq": frame["seq"], "frame_hash": frame["frame_hash"],
            "chain_digest": NB.chain_digest(chain[:index + 1]),
            "stream_id": self.ids[slug],
        }}} for index, frame in enumerate(chain)]

    def write_chain(self, chain, slug="copilot"):
        NB.chain_path(slug).write_text(
            "".join(json.dumps(frame) + "\n" for frame in chain), encoding="utf-8")

    def write_anchors(self):
        NB.ANCHORS.write_text(
            "\n" + "".join(json.dumps(row) + "\n" for row in self.anchors)
            + "not JSON\n", encoding="utf-8")

    def parity(self):
        expected = legacy_check()
        actual = NB.check_anchors()
        self.assertEqual(expected, actual)
        return actual["copilot"]

    def rewrite_interior(self):
        rewritten = copy.deepcopy(self.chain)
        rewritten[7]["payload"]["value"] = "BBBB"
        for index in (7, 8):
            old = rewritten[index]
            rewritten[index] = rapp.build_frame(
                old["kind"], old["stream_id"], old["seq"], old["utc"],
                old["payload"], rewritten[index - 1]["payload_hash"])
        self.assertEqual(self.chain[-1], rewritten[-1], "mutation must preserve the head")
        return rewritten

    def test_all_prefix_digests_are_byte_compatible_with_reference(self):
        frames = self.chain + [{"frame_hash": 'quoted " newline\n snowman \u2603'},
                               {"frame_hash": None}, {"frame_hash": 3}]
        lengths = list(range(len(frames) + 1)) + [len(frames) + 5]
        actual = NB._chain_prefix_digests(frames, lengths)
        expected = {length: NB.chain_digest(frames[:length])
                    for length in range(len(frames) + 1)}
        self.assertEqual(expected, actual)
        self.assertEqual({}, NB._chain_prefix_digests(frames, []))

    def test_good_chain_append_and_multiple_neighbors_match(self):
        result = self.parity()
        self.assertFalse(result["revised"])
        extended = self.frames(36)
        self.write_chain(extended)
        self.assertFalse(self.parity()["revised"])
        other = self.frames(7, slug="other")
        self.write_chain(other, slug="other")
        self.anchors.extend(self.anchor_rows(other, slug="other"))
        self.write_anchors()
        self.parity()
        self.assertFalse(NB.check_anchors()["other"]["revised"])

    def test_resealed_interior_with_unchanged_head_is_detected(self):
        self.write_chain(self.rewrite_interior())
        self.assertTrue(NB.verify("copilot")[0], "resealed mutation should verify")
        result = self.parity()
        self.assertTrue(result["revised"])
        self.assertEqual(7, result["revised_before_seq"])
        self.assertFalse(result["truncated"])
        self.assertTrue(result["witnessed_head_present"])

    def test_same_size_same_mtime_rewrite_cannot_hit_a_stale_cache(self):
        self.assertFalse(NB.check_anchors()["copilot"]["revised"])
        path = NB.chain_path("copilot")
        original = path.stat()
        self.write_chain(self.rewrite_interior())
        self.assertEqual(original.st_size, path.stat().st_size)
        os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
        self.assertEqual(original.st_mtime_ns, path.stat().st_mtime_ns)
        self.assertTrue(self.parity()["revised"])

    def test_new_honest_anchor_cannot_erase_an_older_dispute(self):
        self.write_chain(self.rewrite_interior())
        NB.anchor_heads()
        self.assertTrue(self.parity()["revised"])

    def test_conflicting_duplicate_sequence_observations_are_all_checked(self):
        disputed = copy.deepcopy(self.anchors[3])
        disputed["heads"]["copilot"]["chain_digest"] = "0" * 64
        self.anchors.insert(0, disputed)
        self.write_anchors()
        self.assertEqual(3, self.parity()["revised_before_seq"])
        self.anchors.reverse()
        self.write_anchors()
        self.assertEqual(3, self.parity()["revised_before_seq"])

    def test_noninteger_anchor_sequence_cannot_silently_skip_verification(self):
        for sequence in (3.0, 3.5):
            with self.subTest(sequence=sequence):
                self.anchors[3]["heads"]["copilot"]["seq"] = sequence
                self.write_anchors()
                with self.assertRaises(TypeError):
                    legacy_check()
                with self.assertRaisesRegex(TypeError, "must be integers"):
                    NB.check_anchors()

    def test_truncation_and_disappearance_match(self):
        self.write_chain(self.chain[:-4])
        self.assertTrue(self.parity()["truncated"])
        NB.chain_path("copilot").unlink()
        self.assertTrue(self.parity()["truncated"])

    def test_old_anchors_without_digests_remain_compatible(self):
        for row in self.anchors:
            row["heads"]["copilot"].pop("chain_digest")
        self.write_anchors()
        self.assertFalse(self.parity()["revised"])
        NB.ANCHORS.unlink()
        self.assertEqual({}, NB.check_anchors())

    def test_payload_tampering_still_fails_full_genesis_verification(self):
        with mock.patch.object(rapp, "verify_frame", wraps=rapp.verify_frame) as verify:
            self.assertTrue(NB.verify("copilot")[0])
            self.assertEqual(len(self.chain), verify.call_count)
        self.chain[1]["payload"]["value"] = "BBBB"
        self.write_chain(self.chain)
        self.assertFalse(NB.verify("copilot")[0])
        self.assertFalse(NB.roll_call()["copilot"]["chain_ok"])
        NB.chain_path("copilot").write_text("{broken JSON\n", encoding="utf-8")
        self.assertFalse(NB.roll_call()["copilot"]["chain_ok"])

    def test_hashing_work_is_linear_not_one_entire_prefix_per_anchor(self):
        chain = self.frames(256)
        self.write_chain(chain)
        self.anchors = self.anchor_rows(chain)
        self.anchors.extend(copy.deepcopy(self.anchors))
        self.write_anchors()
        canonical = rapp.canonical
        hashes_visited = 0

        def count(value):
            nonlocal hashes_visited
            if isinstance(value, str) and rapp._HEX64.match(value):
                hashes_visited += 1
            return canonical(value)

        with mock.patch.object(rapp, "canonical", side_effect=count):
            result = NB.check_anchors()
        self.assertFalse(result["copilot"]["revised"])
        self.assertEqual(len(chain), hashes_visited,
                         "each frame hash must be serialized only once")
        print("linear-work proof: %d frames, %d anchors, %d hashes visited" %
              (len(chain), len(self.anchors), hashes_visited))


if __name__ == "__main__":
    unittest.main(verbosity=2)
