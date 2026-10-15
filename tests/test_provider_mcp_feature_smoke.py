"""Offline feature controller tests; no provider or remote service is invoked."""
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_handoff.output import result
from scripts.provider_bootstrap_smoke import EvidenceError
from scripts.provider_feature_mcp_profile import MODEL, TOOL_NAMES
from scripts import provider_mcp_feature_smoke as controller
from scripts.provider_feature_broker import FeatureBroker

ROOT = Path(__file__).resolve().parents[1]
OLD = b"def answer():\n    return 41\n"
NEW = "def answer():\n    return 42\n"
OLD_DIGEST = hashlib.sha256(OLD).hexdigest()
NEW_DIGEST = hashlib.sha256(NEW.encode()).hexdigest()
OP = "1" * 32


class FeatureControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.managed = self.root / "private/repos/managed"

    def _step(self, identity, name, args, value, failed=False):
        text = value if isinstance(value, str) else json.dumps(value, separators=(",", ":"))
        return [{"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "id": identity, "name": name, "input": args}]}},
                {"type": "user", "message": {"content": [
                    {"type": "tool_result", "tool_use_id": identity,
                     "content": [{"type": "text", "text": text}], "is_error": failed}]}}]

    def _write_value(self, status="completed", digest=NEW_DIGEST):
        return {"status": status, "path": "app.py", "sha256": digest, "operation_id": OP}

    def _steps(self, *, write=None, extra_after_resume=(), project_first=False, expected=OLD_DIGEST):
        instructions = ROOT / "template/.agent/instructions.md"
        inspect = result("inspect", inspection={"observed_revision": "a" * 40, "owner_present": False,
                                                "runtime_status": "working", "protocol_version": "1.2"})
        resume = result("resume", status="ready", ownership="owned", observed_revision="b" * 40,
                        managed_checkout=str(self.managed), preservation_handle="c" * 32)
        read = {"path": "app.py", "content": OLD.decode(), "sha256": OLD_DIGEST}
        write_args = {"action": "write", "path": "app.py", "content": NEW, "expected_sha256": expected}
        steps = [("read", "mcp__handoff__read_file", {"path": str(instructions)}, instructions.read_text()),
                 ("inspect", "mcp__handoff__agentctl", {"argv": ["inspect"]}, inspect)]
        project = [("pread", "mcp__handoff__project_file", {"action": "read", "path": "app.py"}, read)]
        resume_step = [("resume", "mcp__handoff__agentctl", {"argv": ["resume"]}, resume)]
        steps += (project + resume_step) if project_first else (resume_step + project)
        steps += list(extra_after_resume)
        steps.append(("pwrite", "mcp__handoff__project_file", write_args, write or self._write_value()))
        return steps

    def _resolve(self, outcome="applied"):
        return ("presolve", "mcp__handoff__project_file", {"action": "resolve", "operation_id": OP},
                {"status": "completed", "path": "app.py", "sha256": NEW_DIGEST, "operation_id": OP,
                 "outcome": outcome})

    def _events(self, steps, failed=()):
        events = [{"type": "system", "subtype": "init", "model": MODEL, "tools": sorted(TOOL_NAMES),
                   "mcp_servers": [{"name": "handoff", "status": "connected"}]}]
        for identity, name, args, value in steps:
            events += self._step(identity, name, args, value, identity in failed)
        events.append({"type": "result", "subtype": "success", "is_error": False,
                       "modelUsage": {MODEL: {"inputTokens": 1, "outputTokens": 1}}})
        return events

    def _evaluate(self, steps, *, failed=(), log=None, resume_seen=True):
        agent_log, doppler_log = self.root / "agent.jsonl", self.root / "doppler.jsonl"
        # Engine log entries carry no origin field.
        entries = log if log is not None else [
            {"phase": "inspect", "status": "completed", "exit_code": 0},
            {"phase": "resume", "status": "ready", "ownership": "owned", "exit_code": 0,
             "managed_checkout": str(self.managed)}]
        agent_log.write_text("".join(json.dumps(entry) + "\n" for entry in entries))
        doppler_log.write_text("")
        config = {"agent_log": str(agent_log), "doppler_log": str(doppler_log),
                  "managed": str(self.managed), "resume_seen": resume_seen,
                  "source": str(ROOT / "template")}
        raw = ("\n".join(json.dumps(event) for event in self._events(steps, failed)) + "\n").encode()
        return controller._evaluate(raw, b"", "synthetic-private-context-marker", config)

    def test_success_uses_origin_free_log_and_returns_applied_write(self):
        _events, _normalized, proof = self._evaluate(self._steps())
        self.assertEqual(proof["resume_count"], 1)
        self.assertEqual(proof["applied"]["value"]["operation_id"], OP)
        self.assertEqual(proof["applied"]["outcome"], "applied")

    def test_engine_log_must_match_native_agent_calls_exactly(self):
        extra = [{"phase": "inspect", "status": "completed", "exit_code": 0},
                 {"phase": "resume", "status": "ready", "ownership": "owned", "exit_code": 0,
                  "managed_checkout": str(self.managed)},
                 {"phase": "resume", "status": "ready", "ownership": "owned", "exit_code": 0,
                  "managed_checkout": str(self.managed)}]
        with self.assertRaisesRegex(EvidenceError, "feature_engine_log_mismatch"):
            self._evaluate(self._steps(), log=extra)
        denied = [{"phase": "inspect", "status": "denied"}]
        with self.assertRaisesRegex(EvidenceError, "feature_engine_unexpected_phase"):
            self._evaluate(self._steps(), log=denied)

    def test_project_file_use_before_owned_resume_completes_is_rejected(self):
        with self.assertRaisesRegex(EvidenceError, "feature_tool_before_ready"):
            self._evaluate(self._steps(project_first=True))

    def test_expected_digest_must_come_from_a_prior_read(self):
        with self.assertRaisesRegex(EvidenceError, "feature_expected_digest_unproven"):
            self._evaluate(self._steps(expected="0" * 64))

    def test_unknown_write_requires_matching_applied_resolution(self):
        unknown = self._steps(write=self._write_value("unknown"))
        with self.assertRaisesRegex(EvidenceError, "feature_write_resolution_unproven"):
            self._evaluate(unknown)
        with self.assertRaisesRegex(EvidenceError, "feature_read_write_unproven"):
            self._evaluate(unknown + [self._resolve("not_applied")])
        _e, _n, proof = self._evaluate(unknown + [self._resolve("applied")])
        self.assertEqual(proof["applied"]["outcome"], "applied")
        self.assertEqual(proof["project_write_count"], 1)

    def test_broker_denied_calls_are_ignored_but_never_count_as_progress(self):
        denied = ("pbad", "mcp__handoff__project_file",
                  {"action": "write", "path": ".env", "content": "x", "expected_sha256": "0" * 64}, "denied")
        _e, _n, proof = self._evaluate(self._steps(extra_after_resume=[denied]), failed={"pbad"})
        self.assertEqual(proof["project_write_count"], 1)

    def test_result_before_use_correlation_comes_from_raw_event_order(self):
        events = self._events(self._steps())
        normalized = controller.claude_feature_trace(events, MODEL)
        items = controller._tool_items(normalized, events)
        self.assertTrue(all(use < done for use, done, _item in items))
        self.assertEqual([use for use, _d, _i in items], sorted(use for use, _d, _i in items))
        with self.assertRaises(EvidenceError):
            controller._tool_items(normalized, events[:3])

    def test_target_ast_is_strict(self):
        self.assertTrue(controller._app_is_answer_42(NEW.encode()))
        for bad in ("def answer():\n    return 41\n", "def answer():\n    return 42.0\n",
                    "def answer():\n    return True\n", "def answer(x):\n    return 42\n",
                    "def answer():\n    return 42\nimport os\n", "answer = 42\n",
                    "def answer():\n    '''d'''\n    return 42\n",
                    "def answer(x, /):\n    return 42\n",
                    "def answer() -> dangerous():\n    return 42\n"):
            self.assertFalse(controller._app_is_answer_42(bad.encode()), bad)
        self.assertFalse(controller._app_is_answer_42(b"\xff"))

    def test_multiple_applied_edits_are_allowed(self):
        steps = self._steps()
        steps += [("pread2", "mcp__handoff__project_file", {"action": "read", "path": "app.py"},
                   {"path": "app.py", "content": NEW, "sha256": NEW_DIGEST}),
                  ("pwrite2", "mcp__handoff__project_file",
                   {"action": "write", "path": "app.py", "content": NEW,
                    "expected_sha256": NEW_DIGEST}, self._write_value())]
        self.assertEqual(self._evaluate(steps)[2]["project_write_count"], 2)

    def test_canonical_read_must_belong_to_fixture(self):
        steps = self._steps()
        identity, name, args, value = steps[0]
        steps[0] = (identity, name, {"path": "/other/.agent/instructions.md"}, value)
        with self.assertRaisesRegex(EvidenceError, "canonical_discovery_order_unproven"):
            self._evaluate(steps)

    def test_real_git_controller_with_intermediate_edit_and_journal_tamper(self):
        # Native events are synthetic; all broker/engine/ownership/edit operations are real.
        cli = self.root / "agentctl"
        cli.write_text(f"#!{sys.executable}\nimport sys\nsys.path.insert(0, {str(ROOT / 'src')!r})\n"
                       "from agent_handoff.cli import main\nraise SystemExit(main())\n")
        cli.chmod(0o700)
        fixture = controller.prepare_fixture(cli)
        root, paths, auth_config, base_config, config = fixture
        self.addCleanup(shutil.rmtree, root)
        wrapper = root / "broker-private/feature.json"
        controller._make_feature_config(wrapper, base_config, config, paths)
        broker = FeatureBroker(wrapper)
        steps = []

        def invoke(identity, name, args):
            reply = broker.call_tool(name, args)
            steps.append((identity, "mcp__handoff__" + name, args, reply))
            return json.loads(reply) if name != "read_file" else reply

        invoke("canonical", "read_file", {"path": str(paths["source"] / ".agent/instructions.md")})
        invoke("inspect", "agentctl", {"argv": ["inspect"]})
        invoke("resume", "agentctl", {"argv": ["resume"]})
        before = invoke("read1", "project_file", {"action": "read", "path": "app.py"})
        invoke("write1", "project_file", {"action": "write", "path": "app.py",
               "content": "def answer():\n    return 40\n", "expected_sha256": before["sha256"]})
        before = invoke("read2", "project_file", {"action": "read", "path": "app.py"})
        invoke("write2", "project_file", {"action": "write", "path": "app.py", "content": NEW,
               "expected_sha256": before["sha256"]})
        raw = ("\n".join(json.dumps(event) for event in self._events(steps)) + "\n").encode()
        token = controller.read_context(paths["actor_private"], paths["source"])
        cfg = controller._safe_config(auth_config)
        proof = controller._evaluate(raw, b"", token, cfg)[2]
        self.assertEqual(proof["project_write_count"], 2)
        self.assertTrue(controller._final_feature_state(paths, cfg, token)["managed_app_answer_42"])
        for write in proof["writes"]:
            controller._feature_journal(paths["actor_private"], Path(cfg["managed"]),
                                        paths["authority"], token, write)
        with patch.object(controller, "prepare_fixture", return_value=fixture), \
                patch.object(controller, "capture_turn", return_value=(0, raw, b"")), \
                patch.object(controller, "provider_settings_snapshot", return_value={}), \
                patch.object(controller, "snapshot_host_config", return_value={}), \
                patch.object(controller, "EVIDENCE_POINTER", self.root / "pointer"):
            outcome = controller.run(cli, cli, run_model=True)
        self.assertTrue(outcome["mechanical_scenario_passed"], outcome)
        self.assertFalse(outcome["agent_interaction_verified"])
        with patch.object(controller, "prepare_fixture", return_value=fixture), \
                patch.object(controller, "capture_turn", return_value=(1, raw, b"")), \
                patch.object(controller, "write_feature_mcp_config", return_value=root / "feature-mcp.json"), \
                patch.object(controller, "provider_settings_snapshot", return_value={}), \
                patch.object(controller, "snapshot_host_config", return_value={}), \
                patch.object(controller, "EVIDENCE_POINTER", self.root / "pointer"):
            failure = controller.run(cli, cli, run_model=True)
        self.assertEqual(failure["failure_category"], "provider_process_failed")
        self.assertTrue(failure["preservation_checks"]["owner_capability_proved"])
        self.assertTrue(failure["preservation_checks"]["source_index_unchanged"])
        forged = dict(proof["writes"][-1], args=dict(proof["writes"][-1]["args"], content="forged"))
        with self.assertRaisesRegex(EvidenceError, "feature_journal_unproven"):
            controller._feature_journal(paths["actor_private"], Path(cfg["managed"]),
                                        paths["authority"], token, forged)

    def test_secret_in_synthetic_doppler_error_stays_out_of_mcp_result_and_logs(self):
        # Native events are synthetic; the local Git, approved binding, broker, engine,
        # Doppler proxy, and child executable are exercised for real.
        cli = self.root / "agentctl"
        cli.write_text(f"#!{sys.executable}\nimport sys\nsys.path.insert(0, {str(ROOT / 'src')!r})\n"
                       "from agent_handoff.cli import main\nraise SystemExit(main())\n")
        cli.chmod(0o700)
        root, paths, auth_config, base_config, config = controller.prepare_fixture(cli)
        self.addCleanup(shutil.rmtree, root)

        sentinel = "A54_NATIVE_MCP_PROVIDER_ERROR_SENTINEL_2f8c"
        executed_marker = root / "synthetic-doppler-executed"
        fake_doppler = Path(config["real_doppler"])
        fake_doppler.write_text(
            f"#!{sys.executable}\nimport pathlib, sys\n"
            "if '--version' in sys.argv:\n print('Doppler CLI 3.77.0'); raise SystemExit(0)\n"
            "if 'get' in sys.argv:\n"
            f" pathlib.Path({str(executed_marker)!r}).write_text('ran\\n')\n"
            f" sys.stdout.write({sentinel!r} + '\\n')\n"
            f" sys.stderr.write({sentinel!r} + '\\n')\n"
            " raise SystemExit(1)\n"
            "raise SystemExit(0)\n",
            encoding="utf-8",
        )
        fake_doppler.chmod(0o700)

        wrapper = root / "broker-private/feature.json"
        controller._make_feature_config(wrapper, base_config, config, paths)
        broker = FeatureBroker(wrapper)
        steps = []

        def invoke(identity, name, args):
            reply = broker.call_tool(name, args)
            steps.append((identity, "mcp__handoff__" + name, args, reply))
            return reply

        invoke("canonical", "read_file", {"path": str(paths["source"] / ".agent/instructions.md")})
        invoke("inspect", "agentctl", {"argv": ["inspect"]})
        invoke("resume", "agentctl", {"argv": ["resume"]})
        result_text = invoke("secrets-inspect", "agentctl", {"argv": ["secrets", "inspect"]})
        self.assertTrue(executed_marker.is_file(), "synthetic Doppler executable was not reached")
        self.assertEqual(executed_marker.read_text(encoding="utf-8"), "ran\n")

        public_result = json.loads(result_text)
        self.assertEqual(public_result["command"], "secrets inspect")
        self.assertEqual(public_result["shared_read"], "unavailable")
        self.assertFalse(public_result["secrets_shared"])
        self.assertNotIn(sentinel, result_text)
        # This native JSONL is assembled from actual broker replies, not an actual model trace.
        native_jsonl = ("\n".join(json.dumps(event) for event in self._events(steps)) + "\n").encode()
        self.assertNotIn(sentinel.encode(), native_jsonl)

        for log_name in (config["agent_log"], config["doppler_log"]):
            self.assertNotIn(sentinel.encode(), Path(log_name).read_bytes())

        private = Path(paths["actor_private"])
        for journal in private.rglob("*.json"):
            if journal.name.startswith(("operation-", "secret-operation-")):
                self.assertNotIn(sentinel.encode(), journal.read_bytes(), str(journal))

        remote = str(paths["remote"])
        object_ids = controller._git("--git-dir", remote, "rev-list", "--objects", "--all").splitlines()
        for entry in object_ids:
            oid = entry.split(maxsplit=1)[0].decode("ascii")
            content = controller._git("--git-dir", remote, "cat-file", "-p", oid)
            self.assertNotIn(sentinel.encode(), content, oid)

        token = controller.read_context(paths["actor_private"], paths["source"])
        cfg = controller._safe_config(auth_config)
        controller._source_guard(paths)
        controller._mainline_guard(paths)
        self.assertTrue(controller._publication_and_owner_guard(paths, cfg, token))

    def test_private_output_is_scanned_before_native_adapter(self):
        token = "private-context-marker"
        for raw, stderr in ((token.encode(), b""), (b'"private\\u002dcontext-marker"', b""),
                            (b"{}", token.encode())):
            with patch.object(controller, "claude_feature_trace") as adapter:
                with self.assertRaises(EvidenceError):
                    controller._evaluate(raw, stderr, token, {})
                adapter.assert_not_called()

    def test_preservation_guards_are_independent(self):
        def fail(*_args):
            raise EvidenceError("x")
        with patch.object(controller, "_source_guard", side_effect=fail), \
                patch.object(controller, "_mainline_guard"), \
                patch.object(controller, "_publication_and_owner_guard", return_value=True):
            checks = controller._preservation({}, {}, "t")
        self.assertFalse(checks["source_unchanged"])
        self.assertFalse(checks["source_index_unchanged"])
        self.assertTrue(checks["mainline_unchanged"])
        self.assertTrue(checks["owner_capability_proved"])
        with patch.object(controller, "_source_guard"), patch.object(controller, "_mainline_guard"), \
                patch.object(controller, "_publication_and_owner_guard", side_effect=fail):
            checks = controller._preservation({}, {}, "t")
        self.assertTrue(checks["source_unchanged"] and checks["mainline_unchanged"])
        self.assertFalse(checks["owner_capability_proved"])
        self.assertFalse(checks["no_feature_publication"])

    def test_model_requires_explicit_opt_in_and_fixed_model(self):
        with self.assertRaisesRegex(EvidenceError, "model_execution_requires_explicit_opt_in"):
            controller.run("/usr/bin/env", "/usr/bin/env")
        with self.assertRaisesRegex(EvidenceError, "claude_model_not_approved"):
            controller.run("/usr/bin/env", "/usr/bin/env", "other", run_model=True)


if __name__ == "__main__":
    unittest.main()
