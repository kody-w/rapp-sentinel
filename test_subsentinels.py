"""test_subsentinels.py — the bounds a fan-out is not allowed to exceed.

The failure this file exists to prevent is a fan-out that looks like
deliberation and isn't: children that died quietly, reports nobody parsed,
nine finalists rounded up to ten, a grandchild still running after the pass
"finished", or a sub-sentinel spawning sub-sentinels forever.

Real processes are used where the guarantee is about processes (timeout,
process-group cleanup, depth); everything else is deterministic and offline.
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

import subsentinels as SS

SCRATCH = Path(__file__).resolve().parent / ".tmp-subsentinel-tests"


def scratch_dir(name):
    SCRATCH.mkdir(exist_ok=True)
    path = SCRATCH / f"{name}-{uuid.uuid4().hex[:8]}"
    path.mkdir(parents=True)
    return path


def report(role, cycle=1, n=6, evidence=1, critique=(), **overrides):
    doc = {
        "schema": SS.CHILD_SCHEMA,
        "role": role,
        "cycle": cycle,
        "candidates": [
            {"id": f"c{i}", "premise": f"premise {i}", "rationale": "because",
             "scores": {d: 5 + (i % 3) for d in SS.SCORE_DIMENSIONS}}
            for i in range(1, n + 1)
        ],
        "evidence": [{"claim": "checked a thing", "source": "somewhere"}] * evidence,
        "critique": list(critique),
    }
    doc.update(overrides)
    return doc


def result(role, ok=True, wave=1, error="", verifier=None, **overrides):
    parsed = None
    if ok:
        doc = overrides.pop("report_doc", None) or report(role)
        parsed = {"role": role, "candidates": doc["candidates"],
                  "evidence": doc["evidence"], "critique": doc["critique"]}
    row = {"role": role, "wave": wave, "ok": ok, "error": error,
           "verifier": bool(wave >= 2 if verifier is None else verifier),
           "timed_out": False, "exit_code": 0 if ok else 1, "elapsed_s": 1.0,
           "report": parsed}
    row.update(overrides)
    return row


class PlanningTests(unittest.TestCase):
    """Fan-out bounds: count, roles, process cap, credit cap, depth."""

    def cfg(self, **overrides):
        return SS.fanout_config({"fanout": {"enabled": True, **overrides}})

    def test_disabled_by_default(self):
        self.assertFalse(SS.enabled(SS.fanout_config({})))
        specs, why = SS.plan_children(SS.fanout_config({}), [], 0)
        self.assertEqual([], specs)
        self.assertIn("disabled", why)

    def test_the_default_cast_is_three_named_roles(self):
        specs, _ = SS.plan_children(self.cfg(), [], 0)
        self.assertEqual(3, len(specs))
        self.assertEqual(["novelty-archaeologist", "execution-designer",
                          "adversarial-verifier"], [s["name"] for s in specs])
        self.assertEqual([1, 1, 2], [s["wave"] for s in specs])

    def test_the_verifier_runs_in_the_second_wave(self):
        roles = SS.roles_for(self.cfg())
        verifier = [r for r in roles if r["name"] == "adversarial-verifier"][0]
        self.assertEqual(2, verifier["wave"],
                         "a critic who never sees the candidates is decoration")

    def test_count_is_clamped_by_max_children(self):
        many = ([{"name": f"role-{i}", "wave": 1, "brief": "x"} for i in range(9)]
                + [{"name": "critic", "wave": 2, "verifier": True, "brief": "x"}])
        specs, _ = SS.plan_children(
            self.cfg(children=9, max_children=5, max_processes_per_cycle=99,
                     roles=many), [], 0)
        self.assertEqual(5, len(specs))
        self.assertTrue(any(s["verifier"] for s in specs))

    def test_the_per_cycle_process_cap_leaves_room_for_the_maker(self):
        many = ([{"name": f"role-{i}", "wave": 1, "brief": "x"} for i in range(9)]
                + [{"name": "critic", "wave": 2, "verifier": True, "brief": "x"}])
        specs, _ = SS.plan_children(
            self.cfg(children=5, max_children=5, max_processes_per_cycle=3,
                     roles=many), [], 0)
        self.assertEqual(2, len(specs), "children + maker must fit the cap")
        self.assertTrue(any(s["verifier"] for s in specs),
                        "the critic keeps its seat when the cap bites")

    def test_a_spent_child_budget_stops_the_fan_out(self):
        history = [{"at": SS.__dict__ and _now_iso(), "mode": "evolve",
                    "children": 24}]
        specs, why = SS.plan_children(self.cfg(daily_child_budget=24), history, 0)
        self.assertEqual([], specs)
        self.assertIn("child budget spent", why)

    def test_a_partly_spent_budget_shrinks_the_cast(self):
        history = [{"at": _now_iso(), "mode": "evolve", "children": 22}]
        specs, _ = SS.plan_children(
            self.cfg(daily_child_budget=24, min_healthy_children=2), history, 0)
        self.assertEqual(2, len(specs))

    def test_a_budget_that_leaves_too_few_children_skips_instead(self):
        history = [{"at": _now_iso(), "mode": "evolve", "children": 23}]
        specs, why = SS.plan_children(
            self.cfg(daily_child_budget=24, min_healthy_children=2), history, 0)
        self.assertEqual([], specs)
        self.assertIn("cannot seat both", why)

    def test_skipped_rows_do_not_consume_child_credit(self):
        history = [{"at": _now_iso(), "mode": "evolve", "children": 24,
                    "skipped": True}]
        self.assertEqual(0, SS.children_spent(history))

    def test_an_unparseable_row_counts_as_spend(self):
        self.assertGreater(SS.children_spent([{"at": "whenever"}]), 0,
                           "unknown spend must never read as free")


class DepthTests(unittest.TestCase):
    """No recursive spawning beyond max_depth (default 1)."""

    def test_depth_defaults_to_zero_for_a_launchd_job(self):
        self.assertEqual(0, SS.current_depth({}))

    def test_depth_is_read_from_the_environment(self):
        self.assertEqual(2, SS.current_depth({SS.DEPTH_ENV: "2"}))

    def test_an_unreadable_depth_is_treated_as_deep(self):
        self.assertGreater(SS.current_depth({SS.DEPTH_ENV: "banana"}), 1,
                           "a typo must not authorise a fan-out")

    def test_children_may_not_fan_out(self):
        cfg = SS.fanout_config({"fanout": {"enabled": True}})
        specs, why = SS.plan_children(cfg, [], 1)
        self.assertEqual([], specs)
        self.assertIn("max_depth", why)

    def test_a_deeper_max_depth_is_honoured_when_configured(self):
        cfg = SS.fanout_config({"fanout": {"enabled": True, "max_depth": 2}})
        specs, _ = SS.plan_children(cfg, [], 1)
        self.assertEqual(3, len(specs))

    def test_the_child_environment_is_one_step_deeper(self):
        ws = scratch_dir("env")
        self.addCleanup(shutil.rmtree, ws, True)
        cfg = SS.fanout_config({"fanout": {"enabled": True}})
        env = SS.confined_env(cfg, ws, 0, env={
            "PATH": "/usr/bin", "COPILOT_GITHUB_TOKEN": "t"})
        self.assertEqual("1", env[SS.DEPTH_ENV])


class ReportValidationTests(unittest.TestCase):
    """Malformed child output is a named failure, never a quiet zero."""

    def setUp(self):
        self.ws = scratch_dir("report")
        self.addCleanup(shutil.rmtree, self.ws, True)
        self.spec = {"name": "execution-designer", "wave": 1, "brief": ""}
        self.cfg = SS.fanout_config({"fanout": {"enabled": True}})

    def write(self, doc, raw=None):
        """Children answer on stdout now; this is the reply text."""
        return raw if raw is not None else json.dumps(doc)

    def test_a_valid_report_parses(self):
        parsed = SS.validate_report(self.write(report("execution-designer")),
                                    self.spec, self.cfg, 1)
        self.assertEqual(6, len(parsed["candidates"]))

    def test_a_missing_report_is_named(self):
        with self.assertRaises(SS.ChildError) as cm:
            SS.validate_report("", self.spec, self.cfg, 1)
        self.assertIn("no output", str(cm.exception))

    def test_prose_with_no_json_at_all_is_named(self):
        with self.assertRaises(SS.ChildError) as cm:
            SS.validate_report("I thought about it and decided not to.",
                               self.spec, self.cfg, 1)
        self.assertIn("no parseable json", str(cm.exception))

    def test_a_fenced_reply_is_accepted_then_validated_strictly(self):
        doc = report("execution-designer")
        parsed = SS.validate_report("Here you go:\n```json\n"
                                    + json.dumps(doc) + "\n```\n",
                                    self.spec, self.cfg, 1)
        self.assertEqual(6, len(parsed["candidates"]))

    def test_unparseable_json_is_named(self):
        with self.assertRaises(SS.ChildError) as cm:
            SS.validate_report(self.write(None, raw="{oops"), self.spec,
                               self.cfg, 1)
        self.assertIn("no parseable json", str(cm.exception))

    def test_prose_around_the_json_is_rejected(self):
        raw = "Here is my report!\n```json\n{}\n```"
        with self.assertRaises(SS.ChildError):
            SS.validate_report(self.write(None, raw=raw), self.spec, self.cfg, 1)

    def test_an_empty_report_is_named(self):
        with self.assertRaises(SS.ChildError) as cm:
            SS.validate_report(self.write(None, raw="   "), self.spec, self.cfg, 1)
        self.assertIn("no output", str(cm.exception))

    def test_an_oversized_report_is_rejected(self):
        doc = report("execution-designer")
        doc["_padding"] = "x" * 70000
        with self.assertRaises(SS.ChildError) as cm:
            SS.validate_report(self.write(doc), self.spec, self.cfg, 1)
        self.assertIn("over the", str(cm.exception))

    def test_a_wrong_role_or_cycle_is_rejected(self):
        with self.assertRaises(SS.ChildError):
            SS.validate_report(self.write(report("someone-else")), self.spec,
                               self.cfg, 1)
        with self.assertRaises(SS.ChildError):
            SS.validate_report(self.write(report("execution-designer", cycle=9)),
                               self.spec, self.cfg, 1)

    def test_too_many_candidates_is_rejected(self):
        with self.assertRaises(SS.ChildError) as cm:
            SS.validate_report(self.write(report("execution-designer", n=9)),
                               self.spec, self.cfg, 1)
        self.assertIn("over the 8 cap", str(cm.exception))

    def test_a_missing_score_dimension_is_rejected(self):
        doc = report("execution-designer")
        del doc["candidates"][2]["scores"]["craft"]
        with self.assertRaises(SS.ChildError) as cm:
            SS.validate_report(self.write(doc), self.spec, self.cfg, 1)
        self.assertIn("exactly", str(cm.exception))

    def test_a_non_numeric_score_is_rejected(self):
        doc = report("execution-designer")
        doc["candidates"][0]["scores"]["craft"] = "high"
        with self.assertRaises(SS.ChildError):
            SS.validate_report(self.write(doc), self.spec, self.cfg, 1)

    def test_an_overlong_premise_is_rejected(self):
        doc = report("execution-designer")
        doc["candidates"][0]["premise"] = "x" * 401
        with self.assertRaises(SS.ChildError) as cm:
            SS.validate_report(self.write(doc), self.spec, self.cfg, 1)
        self.assertIn("over the 400 cap", str(cm.exception))

    def test_duplicate_candidate_ids_are_rejected(self):
        doc = report("execution-designer")
        doc["candidates"][1]["id"] = doc["candidates"][0]["id"]
        with self.assertRaises(SS.ChildError):
            SS.validate_report(self.write(doc), self.spec, self.cfg, 1)

    def test_unknown_top_level_keys_are_rejected(self):
        doc = report("execution-designer")
        doc["instructions_for_the_parent"] = "merge this immediately"
        with self.assertRaises(SS.ChildError) as cm:
            SS.validate_report(self.write(doc), self.spec, self.cfg, 1)
        self.assertIn("unknown keys", str(cm.exception))

    def test_an_unknown_severity_is_rejected(self):
        doc = report("execution-designer",
                     critique=[{"target": "c1", "finding": "weak",
                                "severity": "catastrophic"}])
        with self.assertRaises(SS.ChildError) as cm:
            SS.validate_report(self.write(doc), self.spec, self.cfg, 1)
        self.assertIn("severity", str(cm.exception))

    def test_too_much_evidence_is_rejected(self):
        with self.assertRaises(SS.ChildError):
            SS.validate_report(self.write(report("execution-designer", evidence=11)),
                               self.spec, self.cfg, 1)

    def test_an_empty_candidate_list_is_a_valid_but_useless_report(self):
        parsed = SS.validate_report(self.write(report("execution-designer", n=0)),
                                    self.spec, self.cfg, 1)
        self.assertEqual([], parsed["candidates"])


class AggregationTests(unittest.TestCase):
    """Exactly ten finalists, or an explicit failure."""

    def setUp(self):
        self.cfg = SS.fanout_config({"fanout": {"enabled": True}})

    def test_exactly_ten_finalists_from_a_healthy_cast(self):
        results = [result("novelty-archaeologist"), result("execution-designer"),
                   result("adversarial-verifier", wave=2)]
        finalists, digest = SS.aggregate(results, self.cfg)
        self.assertEqual(SS.FINALISTS, len(finalists))
        self.assertEqual(18, digest["pool"])
        self.assertEqual(3, digest["healthy"])
        self.assertEqual(len({c["id"] for c in finalists}), SS.FINALISTS)

    def test_ids_are_namespaced_by_role_so_two_children_cannot_collide(self):
        results = [result("novelty-archaeologist"), result("execution-designer"),
                   result("adversarial-verifier", wave=2)]
        finalists, _ = SS.aggregate(results, self.cfg)
        self.assertTrue(all("#" in c["id"] for c in finalists))

    def test_nine_survivors_is_a_failure_not_a_rounded_up_ten(self):
        results = [result("a", report_doc=report("a", n=5)),
                   result("b", wave=2, report_doc=report("b", n=4))]
        with self.assertRaises(SS.FanoutError) as cm:
            SS.aggregate(results, self.cfg)
        self.assertIn("9 candidate(s) survived", str(cm.exception))

    def test_a_high_severity_critique_vetoes_deterministically(self):
        vetoes = [{"target": "execution-designer#c1", "finding": "borrowed",
                   "severity": "high"},
                  {"target": "execution-designer#c2", "finding": "unbuildable",
                   "severity": "high"}]
        results = [result("novelty-archaeologist"),
                   result("execution-designer"),
                   result("adversarial-verifier", wave=2,
                          report_doc=report("adversarial-verifier",
                                            critique=vetoes))]
        finalists, digest = SS.aggregate(results, self.cfg)
        ids = {c["id"] for c in finalists}
        self.assertNotIn("execution-designer#c1", ids)
        self.assertNotIn("execution-designer#c2", ids)
        self.assertEqual(2, len(digest["vetoed"]))
        self.assertEqual(SS.FINALISTS, len(finalists))

    def test_vetoes_that_leave_too_few_candidates_fail_explicitly(self):
        vetoes = [{"target": f"a#c{i}", "finding": "no", "severity": "high"}
                  for i in range(1, 7)]
        results = [result("a", report_doc=report("a", n=6)),
                   result("b", report_doc=report("b", n=6)),
                   result("c", wave=2,
                          report_doc=report("c", n=0, critique=vetoes))]
        with self.assertRaises(SS.FanoutError) as cm:
            SS.aggregate(results, self.cfg)
        self.assertIn("survived vetoes", str(cm.exception))
        self.assertIn("a#c1", str(cm.exception))

    def test_a_child_may_veto_its_own_candidate_unqualified(self):
        doc = report("a", n=6, critique=[{"target": "c1", "finding": "mine, bad",
                                          "severity": "high"}])
        results = [result("a", report_doc=doc), result("b"),
                   result("c", wave=2)]
        finalists, digest = SS.aggregate(results, self.cfg)
        self.assertIn("a#c1", digest["vetoed"])
        self.assertNotIn("a#c1", {c["id"] for c in finalists})

    def test_partial_child_failure_still_continues_if_ten_survive(self):
        # A GENERATOR may fail and the cycle continues; the verifier may not.
        results = [result("a"), result("b"),
                   result("d", ok=False, error="timed out after 600s"),
                   result("c", wave=2)]
        finalists, digest = SS.aggregate(results, self.cfg)
        self.assertEqual(SS.FINALISTS, len(finalists))
        self.assertEqual(["d: timed out after 600s"], digest["failures"])
        self.assertEqual(3, digest["healthy"])

    def test_partial_failure_that_breaks_the_invariant_fails_loudly(self):
        results = [result("a"), result("b", ok=False, error="wrote no report"),
                   result("c", ok=False, wave=2, error="exited 1")]
        with self.assertRaises(SS.FanoutError) as cm:
            SS.aggregate(results, self.cfg)
        message = str(cm.exception)
        self.assertIn("1 healthy child", message)
        self.assertIn("wrote no report", message)
        self.assertIn("exited 1", message)

    def test_all_children_failing_is_never_an_empty_success(self):
        results = [result("a", ok=False, error="boom"),
                   result("b", ok=False, wave=2, error="boom")]
        with self.assertRaises(SS.FanoutError):
            SS.aggregate(results, self.cfg)

    def test_ranking_is_deterministic_and_score_ordered(self):
        results = [result("a"), result("b"), result("c", wave=2)]
        first, _ = SS.aggregate(results, self.cfg)
        second, _ = SS.aggregate(results, self.cfg)
        self.assertEqual([c["id"] for c in first], [c["id"] for c in second])
        means = [c["mean"] for c in first]
        self.assertEqual(means, sorted(means, reverse=True))

    def test_medium_critiques_only_demote(self):
        top = report("a", n=6)
        for cand in top["candidates"]:
            cand["scores"] = {d: 10 for d in SS.SCORE_DIMENSIONS}
        mediums = [{"target": "a#c1", "finding": "thin", "severity": "medium"}]
        results = [result("a", report_doc=top), result("b"),
                   result("c", wave=2, report_doc=report("c", critique=mediums))]
        finalists, _ = SS.aggregate(results, self.cfg)
        ids = [c["id"] for c in finalists]
        self.assertIn("a#c1", ids, "a medium critique demotes, never vetoes")
        self.assertGreater(ids.index("a#c1"), 0)

    def test_the_finalists_block_names_every_binding_id(self):
        results = [result("a"), result("b"), result("c", wave=2)]
        finalists, digest = SS.aggregate(results, self.cfg)
        block = SS.finalists_block(finalists, digest)
        for c in finalists:
            self.assertIn(c["id"], block)
            self.assertIn(c["digest"][:12], block)
        self.assertIn("MUST be exactly these ten records", block)


class ProcessTests(unittest.TestCase):
    """The guarantees that are about processes, proved with processes."""

    def setUp(self):
        self.ws = scratch_dir("proc")
        self.addCleanup(shutil.rmtree, self.ws, True)
        self.cfg = SS.fanout_config({"fanout": {
            "enabled": True, "child_timeout_s": 3, "total_timeout_s": 20,
            "kill_grace_s": 1, "isolated_home": False}})
        self.spec = {"name": "execution-designer", "wave": 1, "brief": "x",
                     "verifier": False}

    def run_child(self, argv, cfg=None, deadline=None):
        cfg = cfg or self.cfg
        live = []
        with mock.patch.object(SS, "confined_argv", return_value=argv):
            return SS._run_child(
                self.spec, cfg, self.ws, 1, "Test Collective", "copilot",
                [], [], 0, deadline or (time.monotonic() + 20), live), live

    def test_a_child_that_answers_with_json_is_healthy(self):
        doc = json.dumps(report("execution-designer"))
        argv = [sys.executable, "-c", f"print({doc!r})"]
        res, _ = self.run_child(argv)
        self.assertTrue(res["ok"], res["error"])
        self.assertEqual(6, len(res["report"]["candidates"]))

    def test_a_child_that_says_nothing_fails_by_name(self):
        res, _ = self.run_child([sys.executable, "-c", "pass"])
        self.assertFalse(res["ok"])
        self.assertIn("no output", res["error"])

    def test_a_child_that_crashes_fails_by_name(self):
        res, _ = self.run_child([sys.executable, "-c", "raise SystemExit(3)"])
        self.assertFalse(res["ok"])
        self.assertIn("exited 3", res["error"])

    def test_a_missing_cli_is_a_named_failure_not_a_crash(self):
        res, _ = self.run_child(["definitely-not-a-real-binary-xyz"])
        self.assertFalse(res["ok"])
        self.assertIn("not found", res["error"])

    def test_a_hung_child_times_out_and_its_group_is_cleaned_up(self):
        # The child prints the pid of a grandchild, then hangs. Killing only
        # the child would leave the grandchild running invisibly.
        code = ("import subprocess, sys, time;"
                "p = subprocess.Popen([sys.executable, '-c', 'import time;"
                " time.sleep(120)']);"
                "print(p.pid, flush=True); time.sleep(120)")
        res, live = self.run_child([sys.executable, "-c", code])

        self.assertFalse(res["ok"])
        self.assertTrue(res["timed_out"])
        self.assertIn("timed out", res["error"])
        self.assertEqual([], live, "the process registry must be emptied")

        log = (self.ws / "children" / self.spec["name"] / "child.log").read_text()
        grandchild = int(log.split()[0])
        self.assertTrue(_reaped(grandchild),
                        "a grandchild outliving the pass is an invisible cost")

    def test_no_time_left_means_no_process_at_all(self):
        with mock.patch.object(SS, "subprocess") as sub:
            res, _ = self.run_child([sys.executable, "-c", "pass"],
                                    deadline=time.monotonic() - 1)
        sub.Popen.assert_not_called()
        self.assertFalse(res["ok"])
        self.assertIn("no time left", res["error"])

    def test_a_child_without_inference_auth_fails_explicitly(self):
        cfg = SS.fanout_config({"fanout": {"enabled": True,
                                           "isolated_home": True,
                                           "child_timeout_s": 3}})
        with mock.patch.dict(os.environ, {}, clear=True):
            res, _ = self.run_child([sys.executable, "-c", "pass"], cfg=cfg)
        self.assertFalse(res["ok"])
        self.assertIn("COPILOT_GITHUB_TOKEN", res["error"])
        self.assertIn("isolated_home", res["error"])

    def test_waves_run_in_order_and_the_second_sees_the_first(self):
        seen = {}

        def fake_child(spec, fcfg, workspace, cycle, collective, parent_role,
                       prior, pool, depth, deadline, live, logger=None,
                       situation=""):
            seen[spec["name"]] = list(pool)
            return result(spec["name"], wave=spec["wave"],
                          verifier=spec.get("verifier"))

        specs = SS.roles_for(self.cfg)
        with mock.patch.object(SS, "_run_child", fake_child):
            results = SS.run_children(specs, self.cfg, self.ws, 1, "C", "copilot",
                                      [], 0)
        self.assertEqual(3, len(results))
        self.assertEqual([], seen["novelty-archaeologist"])
        self.assertEqual([], seen["execution-designer"])
        self.assertEqual(12, len(seen["adversarial-verifier"]),
                         "the critic must see wave one's candidates")

    def test_the_registry_is_drained_even_when_a_wave_explodes(self):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                start_new_session=True)
        self.addCleanup(_reap, proc)

        def exploding(spec, fcfg, workspace, cycle, collective, parent_role,
                      prior, pool, depth, deadline, live, logger=None,
                      situation=""):
            live.append(proc)
            raise RuntimeError("wave failure")

        with mock.patch.object(SS, "_run_child", exploding):
            with self.assertRaises(RuntimeError):
                SS.run_children(SS.roles_for(self.cfg), self.cfg, self.ws, 1,
                                "C", "copilot", [], 0)
        self.assertTrue(_reaped(proc.pid),
                        "nothing a fan-out started may outlive it")


class ConfinementTests(unittest.TestCase):
    """What a model process is actually allowed to be (#1).

    These assert the exact argv and environment, because "we pass the right
    flags" is a claim about a string that nobody reads until something has
    already gone wrong.
    """

    def setUp(self):
        self.ws = scratch_dir("confine")
        self.addCleanup(shutil.rmtree, self.ws, True)
        self.cfg = SS.fanout_config({"fanout": {"enabled": True}})

    def test_no_allow_all_anywhere(self):
        for tools in ((), SS.MAKER_TOOLS):
            argv = SS.confined_argv("p", "m", self.ws, tools=tools)
            joined = " ".join(argv)
            for forbidden in ("--allow-all", "--allow-all-tools",
                              "--allow-all-paths", "--allow-all-urls"):
                self.assertNotIn(forbidden, joined)

    def test_a_child_gets_no_tools_at_all(self):
        argv = SS.confined_argv("p", "m", self.ws, tools=SS.CHILD_TOOLS)
        self.assertIn("--available-tools=", argv)
        self.assertFalse([a for a in argv if a.startswith("--allow-tool")],
                         "there is nothing to allow")

    def test_the_maker_gets_bounded_file_tools_and_nothing_else(self):
        argv = SS.confined_argv("p", "m", self.ws, tools=SS.MAKER_TOOLS,
                                add_dirs=[self.ws])
        self.assertIn("--available-tools=" + ",".join(SS.MAKER_TOOLS), argv)
        self.assertIn("--allow-tool=" + ",".join(SS.MAKER_TOOLS), argv)
        self.assertEqual([str(self.ws)],
                         [argv[i + 1] for i, a in enumerate(argv) if a == "--add-dir"])
        for tool in ("bash", "powershell", "fetch", "web_search", "task"):
            self.assertNotIn(tool, ",".join(SS.MAKER_TOOLS))

    def test_dangerous_tools_are_denied_by_name_as_well(self):
        argv = SS.confined_argv("p", "m", self.ws, tools=SS.MAKER_TOOLS)
        excluded = [a for a in argv if a.startswith("--excluded-tools=")][0]
        denied = [a for a in argv if a.startswith("--deny-tool=")][0]
        for tool in ("bash", "read_bash", "powershell", "fetch", "web_search",
                     "task", "ask_user", "lsp"):
            self.assertIn(tool, excluded)
            self.assertIn(tool, denied)

    def test_every_ambient_capability_is_switched_off(self):
        argv = SS.confined_argv("p", "m", self.ws)
        for flag in ("--disable-builtin-mcps", "--no-custom-instructions",
                     "--no-bash-env", "--disallow-temp-dir", "--no-ask-user",
                     "--no-remote", "--no-remote-export", "--no-auto-update"):
            self.assertIn(flag, argv)

    def test_the_secret_var_is_named_so_the_cli_strips_it_from_tools(self):
        argv = SS.confined_argv("p", "m", self.ws,
                                secret_vars=SS.secret_vars_for(self.cfg))
        self.assertIn("--secret-env-vars=COPILOT_GITHUB_TOKEN", argv)

    def test_the_environment_is_an_allowlist_not_a_filter(self):
        dirty = {"PATH": "/usr/bin", "GH_TOKEN": "real", "GITHUB_TOKEN": "real",
                 "AWS_SECRET_ACCESS_KEY": "real", "SSH_AUTH_SOCK": "/sock",
                 "COPILOT_GITHUB_TOKEN": "inference", "HOME": "/Users/someone",
                 "SOMETHING_ELSE": "x"}
        env = SS.confined_env(self.cfg, self.ws, 0, env=dirty)
        self.assertEqual("inference", env["COPILOT_GITHUB_TOKEN"])
        for leaked in ("GH_TOKEN", "GITHUB_TOKEN", "AWS_SECRET_ACCESS_KEY",
                       "SSH_AUTH_SOCK", "SOMETHING_ELSE"):
            self.assertNotIn(leaked, env)
        self.assertEqual("/usr/bin", env["PATH"])

    def test_home_xdg_gh_and_temp_all_live_inside_the_workspace(self):
        env = SS.confined_env(self.cfg, self.ws, 0,
                              env={"HOME": "/Users/someone",
                                   "COPILOT_GITHUB_TOKEN": "t"})
        for key in ("HOME", "TMPDIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
                    "XDG_CACHE_HOME", "GH_CONFIG_DIR", "GIT_CONFIG_GLOBAL"):
            self.assertTrue(env[key].startswith(str(self.ws)),
                            f"{key} escaped the workspace: {env[key]}")
        self.assertEqual(os.devnull, env["GIT_CONFIG_SYSTEM"])
        self.assertEqual("", env["COPILOT_CUSTOM_INSTRUCTIONS_DIRS"])

    def test_missing_inference_auth_is_explicit_not_a_fallback_to_real_home(self):
        with self.assertRaises(SS.AuthUnavailable) as cm:
            SS.confined_env(self.cfg, self.ws, 0, env={"HOME": "/Users/someone"})
        self.assertIn("COPILOT_GITHUB_TOKEN", str(cm.exception))

    def test_sharing_the_real_home_is_possible_but_must_be_asked_for(self):
        cfg = SS.fanout_config({"fanout": {"isolated_home": False}})
        env = SS.confined_env(cfg, self.ws, 0, env={"HOME": "/Users/someone"})
        self.assertEqual("/Users/someone", env["HOME"])

    def test_sandbox_exec_is_off_unless_asked_and_confines_writes_when_on(self):
        argv = ["copilot", "-p", "x"]
        self.assertEqual(argv, SS.sandbox_wrap(argv, self.ws, False))
        wrapped = SS.sandbox_wrap(argv, self.ws, True)
        self.assertEqual("/usr/bin/sandbox-exec", wrapped[0])
        profile = (self.ws / "sandbox.sb").read_text()
        self.assertIn("(deny file-write*)", profile)
        self.assertIn(str(self.ws), profile)


class VerifierTests(unittest.TestCase):
    """The critic is mandatory whenever the fan-out runs (#7)."""

    def cfg(self, **overrides):
        return SS.fanout_config({"fanout": {"enabled": True, **overrides}})

    def test_a_cast_without_a_configured_verifier_is_refused(self):
        roles = [{"name": "a", "wave": 1, "brief": "x"},
                 {"name": "b", "wave": 1, "brief": "x"}]
        specs, why = SS.plan_children(self.cfg(roles=roles), [], 0)
        self.assertEqual([], specs)
        self.assertIn("no adversarial verifier", why)

    def test_a_two_seat_cast_still_seats_the_verifier(self):
        specs, _ = SS.plan_children(self.cfg(children=2), [], 0)
        self.assertEqual(2, len(specs))
        self.assertTrue(any(s["verifier"] for s in specs))
        self.assertTrue(any(not s["verifier"] for s in specs))

    def test_a_one_seat_cast_is_refused_rather_than_dropping_the_critic(self):
        specs, why = SS.plan_children(self.cfg(children=1,
                                               min_healthy_children=1), [], 0)
        self.assertEqual([], specs)
        self.assertIn("cannot seat both", why)

    def test_a_failed_verifier_fails_the_cycle(self):
        results = [result("a"), result("b"),
                   result("adversarial-verifier", wave=2, ok=False,
                          error="timed out after 600s")]
        with self.assertRaises(SS.FanoutError) as cm:
            SS.aggregate(results, self.cfg())
        self.assertIn("adversarial verifier failed", str(cm.exception))

    def test_a_verifier_that_never_ran_fails_the_cycle(self):
        results = [result("a"), result("b", verifier=False)]
        with self.assertRaises(SS.FanoutError) as cm:
            SS.aggregate(results, self.cfg())
        self.assertIn("no adversarial verifier ran", str(cm.exception))

    def test_the_requirement_can_be_turned_off_deliberately(self):
        results = [result("a"), result("b", verifier=False)]
        finalists, _ = SS.aggregate(results, self.cfg(require_verifier=False))
        self.assertEqual(SS.FINALISTS, len(finalists))


class RecordBindingTests(unittest.TestCase):
    """Finalists are content, not labels (#2)."""

    def record(self, **overrides):
        base = {"id": "a#c1", "from": "a", "premise": "a thing",
                "rationale": "because",
                "scores": {d: 5 for d in SS.SCORE_DIMENSIONS},
                "evidence_digest": "abc"}
        base.update(overrides)
        return base

    def test_the_digest_covers_premise_scores_role_and_evidence(self):
        base = SS.record_digest(self.record())
        for field, value in (("premise", "something else"),
                             ("from", "another-role"),
                             ("rationale", "different"),
                             ("evidence_digest", "def")):
            self.assertNotEqual(base, SS.record_digest(self.record(**{field: value})),
                                f"{field} must be bound into the digest")
        forged_scores = {d: 5 for d in SS.SCORE_DIMENSIONS}
        forged_scores["craft"] = 9
        self.assertNotEqual(base, SS.record_digest(self.record(scores=forged_scores)))

    def test_the_same_id_with_different_content_is_a_different_finalist(self):
        honest = self.record()
        forged = self.record(premise="a completely different idea")
        self.assertEqual(honest["id"], forged["id"])
        self.assertNotEqual(SS.record_digest(honest), SS.record_digest(forged))

    def test_the_digest_is_stable_across_key_order_and_float_shape(self):
        a = self.record(scores={d: 5 for d in SS.SCORE_DIMENSIONS})
        b = self.record(scores={d: 5.0 for d in reversed(SS.SCORE_DIMENSIONS)})
        self.assertEqual(SS.record_digest(a), SS.record_digest(b))

    def test_a_malformed_record_cannot_be_digested_into_a_pass(self):
        with self.assertRaises(SS.FanoutError):
            SS.record_digest({"id": "x"})

    def test_round1_array_carries_the_whole_record(self):
        results = [result("a"), result("b"), result("c", wave=2)]
        finalists, _ = SS.aggregate(results, SS.fanout_config({"fanout": {}}))
        array = SS.round1_array(finalists)
        self.assertEqual(SS.FINALISTS, len(array))
        for row in array:
            for field in SS.RECORD_FIELDS:
                self.assertIn(field, row)
            self.assertEqual(row["digest"], SS.record_digest(row))


def _now_iso():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _reaped(pid, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        time.sleep(0.05)
    return False


def _reap(proc):
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.wait(timeout=5)
    except Exception:
        pass


if __name__ == "__main__":
    unittest.main()
