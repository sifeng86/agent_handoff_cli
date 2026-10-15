"""Offline controller safety tests; no provider or remote service is invoked."""
import json
import os
from contextlib import ExitStack
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from agent_handoff.output import result
from scripts.provider_bootstrap_smoke import EvidenceError
from scripts.provider_mcp_auth_smoke import (
    MODEL, TurnFailure, _evaluate, capture_turn, run, scan_native_output,
)
from scripts.provider_mcp_profile import TOOL_NAMES
from scripts import provider_mcp_auth_smoke as controller


class ProviderMcpAuthControllerTests(unittest.TestCase):
    def _tool_use(self, identity, name, arguments):
        return {"type": "tool_use", "id": identity, "name": name, "input": arguments}

    def _tool_result(self, identity, value):
        if isinstance(value, dict):
            value = json.dumps(value, separators=(",", ":"))
        return {"type": "tool_result", "tool_use_id": identity,
                "content": [{"type": "text", "text": value}], "is_error": False}

    def _native(self, root, *, resume=True):
        canonical = (Path(__file__).resolve().parents[1] / "template/.agent/instructions.md").read_text()
        managed = root / "private/repos/managed"
        inspect = result("inspect", inspection={"observed_revision": "a" * 40, "owner_present": False,
            "runtime_status": "working", "protocol_version": "1.2"})
        resume_result = result("resume", status="ready", ownership="owned", observed_revision="b" * 40,
            managed_checkout=str(managed), preservation_handle="c" * 32)
        verify = result("secrets verify", status="not_ready", secrets_shared=False,
            secrets_mode="doppler", destination_identity="d" * 64, shared_read="unavailable",
            shared_read_failure="authentication_required", availability={"status": "completed",
            "available_keys": [], "required_missing": [], "unsupported_keys": [], "secrets_shared": False},
            convergence="unverified")

        def call(identity, name, args):
            return {"type": "assistant", "message": {"content": [self._tool_use(identity, name, args)]}}

        def reply(identity, value):
            return {"type": "user", "message": {"content": [self._tool_result(identity, value)]}}

        events = [
            {"type": "system", "subtype": "init", "model": MODEL, "tools": sorted(TOOL_NAMES),
             "mcp_servers": [{"name": "handoff", "status": "connected"}]},
            call("read", "mcp__handoff__read_file", {"path": str(Path(__file__).resolve().parents[1] / "template/.agent/instructions.md")}),
            reply("read", canonical),
            call("inspect", "mcp__handoff__agentctl", {"argv": ["inspect"]}), reply("inspect", inspect),
        ]
        if resume:
            events.extend([call("resume", "mcp__handoff__agentctl", {"argv": ["resume"]}),
                           reply("resume", resume_result)])
        events.extend([
            call("verify", "mcp__handoff__agentctl", {"argv": ["secrets", "verify"]}), reply("verify", verify),
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "The check is unavailable."}]}},
            {"type": "result", "subtype": "success", "is_error": False,
             "modelUsage": {MODEL: {"inputTokens": 1, "outputTokens": 1}}},
        ])
        return events

    def _run_fixture(self, root, *, resume=True):
        source, private = root / "source", root / "private"
        source.mkdir(); private.mkdir()
        agent_log, doppler_log = root / "agent.jsonl", root / "doppler.jsonl"
        calls = [{"phase": "inspect", "status": "completed", "exit_code": 0}]
        if resume:
            calls.append({"phase": "resume", "status": "ready", "ownership": "owned", "exit_code": 0})
        calls.extend([
            {"phase": "secrets verify", "status": "not_ready", "exit_code": 6,
             "shared_read": "unavailable", "shared_read_failure": "authentication_required",
             "availability_status": "completed", "required_missing": []},
        ])
        dp_calls = [{"phase": "secrets_get", "origin": "engine", "parent_cli_phase": "secrets verify",
                     "no_credentials_classified": True}]
        agent_log.write_text("".join(json.dumps(call) + "\n" for call in calls))
        doppler_log.write_text("".join(json.dumps(call) + "\n" for call in dp_calls))
        config = {"source": str(source), "private_root": str(private),
                  "managed": str(private / "repos/managed") if resume else None, "resume_seen": resume,
                  "require_auth_failure_reason": True, "agent_log": str(agent_log),
                  "doppler_log": str(doppler_log), "authority": str(root / "authority.git"),
                  "ref": "refs/agent-handoff/test", "context_file": str(private / "context.json")}
        paths = {"source": source, "actor_private": private}
        paths.update(source_head="a" * 40, source_index="b" * 64, base="c" * 40,
                     init_revision="d" * 40)
        return paths, config, calls, dp_calls

    def _mocked_run(self, root, *, capture=None, settings=None, host=None, profile_error=None,
                    final_state_error=None, resume=True):
        paths, config, _calls, _dp_calls = self._run_fixture(root, resume=resume)
        fixture = (root, paths, root / "auth.json", root / "broker.json", config)
        canonical = Path(__file__).resolve().parents[1] / "template/.agent/instructions.md"
        native = ("\n".join(json.dumps(event) for event in self._native(root, resume=resume)) + "\n").encode()
        settings_values = iter(settings or [{"settings": "same"}, {"settings": "same"}])
        host_values = iter(host or [("missing",), ("missing",)])
        guard_calls = {"final_state": 0, "settings": 0, "host": 0}

        def next_value(values, key, *args):
            guard_calls[key] += 1
            value = next(values)
            if isinstance(value, Exception):
                raise value
            return value

        def final_state(*_args):
            guard_calls["final_state"] += 1
            if final_state_error is not None:
                raise final_state_error
            return {"source_unchanged": True, "source_index_unchanged": True, "mainline_unchanged": True,
                    "owner_capability_proved": resume, "no_feature_publication": True,
                    "resume_observed": resume}
        pointer = root / "evidence.pointer"
        with ExitStack() as stack:
            stack.enter_context(patch.object(controller, "prepare_fixture", return_value=fixture))
            stack.enter_context(patch.object(controller, "read_context", return_value="synthetic-private-context-marker"))
            stack.enter_context(patch.object(controller, "provider_settings_snapshot",
                                             side_effect=lambda *args: next_value(settings_values, "settings", *args)))
            stack.enter_context(patch.object(controller, "snapshot_host_config",
                                             side_effect=lambda: next_value(host_values, "host")))
            stack.enter_context(patch.object(controller, "write_mcp_config",
                                              side_effect=profile_error or (lambda destination, _broker: Path(destination))))
            stack.enter_context(patch.object(controller, "claude_mcp_command", return_value=["fake-claude"]))
            capture_mock = stack.enter_context(patch.object(controller, "capture_turn",
                side_effect=capture or (lambda *_args, **_kwargs: (0, native, b""))))
            real_read_json = controller._read_json
            stack.enter_context(patch.object(controller, "_read_json",
                side_effect=lambda path: config if Path(path) == root / "auth.json" else real_read_json(path)))
            final_state_mock = stack.enter_context(patch.object(controller, "_final_state", side_effect=final_state))
            stack.enter_context(patch.object(controller, "EVIDENCE_POINTER", pointer))
            report = run("/usr/bin/env", "/usr/bin/env", run_model=True)
        return report, capture_mock, pointer, config, paths, _calls, guard_calls

    def test_raw_and_decoded_provider_outputs_are_scanned_before_normalization(self):
        token = "synthetic-context-capability-marker"
        with self.assertRaisesRegex(EvidenceError, "context_token_disclosed"):
            scan_native_output((json.dumps({"type": "assistant", "text": token}) + "\n").encode(), token)
        escaped = token.replace("s", r"\u0073", 1)
        raw = ('{"type":"result","message":"' + escaped + '"}\n').encode()
        self.assertNotIn(token.encode(), raw)
        with self.assertRaisesRegex(EvidenceError, "context_token_disclosed"):
            scan_native_output(raw, token)
        # A failed terminal tool result is also part of the native stream and must be scanned.
        failed = {"type": "user", "message": {"content": [{"type": "tool_result", "content": token,
                                                                    "is_error": True}]}}
        with self.assertRaisesRegex(EvidenceError, "context_token_disclosed"):
            scan_native_output((json.dumps(failed) + "\n").encode(), token)

    def test_synthetic_trace_requires_single_owned_resume_auth_and_returns_manual_review_pending(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            token = "synthetic-private-context-marker"
            agent_calls = [
                {"phase": "inspect", "status": "completed", "exit_code": 0},
                {"phase": "resume", "status": "ready", "ownership": "owned", "exit_code": 0},
                {"phase": "secrets verify", "status": "not_ready", "exit_code": 6,
                 "shared_read": "unavailable", "shared_read_failure": "authentication_required",
                 "availability_status": "completed", "required_missing": []},
            ]
            doppler_calls = [{"phase": "secrets_get", "origin": "engine", "parent_cli_phase": "secrets verify",
                              "no_credentials_classified": True}]
            config = {"source": str(root / "source"), "private_root": str(root / "private"),
                      "managed": str(root / "private/repos/managed"), "require_auth_failure_reason": True,
                      "resume_seen": True,
                      "agent_log": str(root / "agent.jsonl"), "doppler_log": str(root / "doppler.jsonl")}
            Path(config["agent_log"]).write_text("".join(json.dumps(call) + "\n" for call in agent_calls))
            Path(config["doppler_log"]).write_text("".join(json.dumps(call) + "\n" for call in doppler_calls))
            raw = ("\n".join(json.dumps(event) for event in self._native(root)) + "\n").encode()
            events, normalized, proof, prose = _evaluate(raw, b"", token, config)
            self.assertTrue(proof["engine_authentication_required_observed"])
            self.assertFalse(proof["direct_missing_token_diagnostic_observed"])
            self.assertIn("unavailable", prose)
            self.assertEqual(sum(call["phase"] == "resume" for call in agent_calls), 1)
            agent_calls.append(dict(agent_calls[1]))
            Path(config["agent_log"]).write_text("".join(json.dumps(call) + "\n" for call in agent_calls))
            with self.assertRaisesRegex(EvidenceError, "single_owned_resume_unproven"):
                _evaluate(raw, b"", token, config)

    def test_readonly_auth_diagnostic_can_pass_without_resume_or_owner_capability(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            report, capture_mock, _pointer, config, _paths, _calls, _guards = self._mocked_run(
                root, resume=False)
            capture_mock.assert_called_once()
            self.assertTrue(report["mechanical_scenario_passed"])
            self.assertTrue(report["synthetic_missing_auth_engine_diagnostic"])
            self.assertFalse(report["resume_observed"])
            self.assertEqual(report["resume_count"], 0)
            self.assertFalse(report["resume_exactly_once_owned"])
            self.assertFalse(report["owner_capability_proved"])
            self.assertTrue(report["initial_free_owner_preserved"])
            self.assertFalse(report["project_mutation_observed"])
            self.assertFalse(config["resume_seen"])
            self.assertIsNone(config["managed"])
            self.assertFalse(report["agent_interaction_verified"])
            self.assertTrue(report["primary_prose_review_required"])
            self.assertIn("readonly auth diagnostic", report["evidence_scope"])
            self.assertIn("no OAuth/2FA completion", report["evidence_scope"])

    def test_resume_state_must_match_proxy_log_and_free_owner_fixture(self):
        token = "synthetic-private-context-marker"
        cases = ("managed_without_resume", "seen_without_resume", "logged_resume_without_config")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                _paths, config, calls, _dp_calls = self._run_fixture(root, resume=False)
                native = self._native(root, resume=False)
                if case == "managed_without_resume":
                    config["managed"] = str(root / "private/repos/managed")
                elif case == "seen_without_resume":
                    config["resume_seen"] = True
                else:
                    resume_root = root / "resume-fixture"
                    resume_root.mkdir()
                    _paths, config, calls, _dp_calls = self._run_fixture(resume_root, resume=True)
                    config["resume_seen"] = False
                    config["managed"] = None
                    native = self._native(resume_root, resume=True)
                raw = ("\n".join(json.dumps(event) for event in native) + "\n").encode()
                with self.assertRaisesRegex(EvidenceError, "resume_state_incoherent"):
                    _evaluate(raw, b"", token, config)

    def test_resume_must_be_owned_and_follow_completed_inspection(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _paths, config, calls, dp_calls = self._run_fixture(root, resume=True)
            calls[1]["status"] = "not_ready"
            calls[1]["exit_code"] = 6
            config["agent_log"] = str(root / "agent.jsonl")
            Path(config["agent_log"]).write_text("".join(json.dumps(call) + "\n" for call in calls))
            config["doppler_log"] = str(root / "doppler.jsonl")
            Path(config["doppler_log"]).write_text("".join(json.dumps(call) + "\n" for call in dp_calls))
            raw = ("\n".join(json.dumps(event) for event in self._native(root, resume=True)) + "\n").encode()
            with self.assertRaisesRegex(EvidenceError, "single_owned_resume_unproven"):
                _evaluate(raw, b"", "synthetic-private-context-marker", config)

            calls[1]["status"] = "ready"
            calls[1]["exit_code"] = 0
            calls[0], calls[1] = calls[1], calls[0]
            Path(config["agent_log"]).write_text("".join(json.dumps(call) + "\n" for call in calls))
            events = self._native(root, resume=True)
            # Keep the canonical read first, but place the logged resume before inspect.
            events[3:7] = events[5:7] + events[3:5]
            raw = ("\n".join(json.dumps(event) for event in events) + "\n").encode()
            with self.assertRaisesRegex(EvidenceError, "inspection_before_resume_unproven"):
                _evaluate(raw, b"", "synthetic-private-context-marker", config)

    def test_model_requires_explicit_opt_in_and_fixed_model(self):
        with self.assertRaisesRegex(EvidenceError, "model_execution_requires_explicit_opt_in"):
            run("/unavailable/agentctl", "/unavailable/claude")
        with self.assertRaisesRegex(EvidenceError, "claude_model_not_approved"):
            run("/unavailable/agentctl", "/unavailable/claude", "fallback", run_model=True)

    def test_combined_output_budget_and_timeout_kill_provider_process_group(self):
        command = [sys.executable, "-c", "import sys; sys.stdout.write('x' * 4096); sys.stdout.flush()"]
        with self.assertRaisesRegex(EvidenceError, "provider_output_limit_exceeded"):
            capture_turn(command, b"", Path.cwd(), timeout=3, limit=128)

        with tempfile.TemporaryDirectory() as temp:
            marker = Path(temp) / "late-child-marker"
            child = "import time; time.sleep(.7); open(%r,'w').write('late')" % str(marker)
            parent = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',%r]); time.sleep(5)" % child
            with self.assertRaisesRegex(EvidenceError, "provider_turn_timeout"):
                capture_turn([sys.executable, "-c", parent], b"", Path.cwd(), timeout=0.15, limit=4096)
            time.sleep(0.8)
            self.assertFalse(marker.exists())

    def test_mocked_success_is_scoped_and_still_requires_primary_prose_review(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            report, capture_mock, pointer, _config, _paths, _calls, _guard_calls = self._mocked_run(root)
            capture_mock.assert_called_once()
            self.assertTrue(report["mechanical_scenario_passed"])
            self.assertFalse(report["agent_interaction_verified"])
            self.assertTrue(report["primary_prose_review_required"])
            self.assertTrue(report["synthetic_missing_auth_engine_diagnostic"])
            self.assertFalse(report["direct_model_doppler_calls"])
            self.assertTrue(pointer.exists())
            baseline = json.loads((root / "initial-git-baseline.json").read_text())
            self.assertEqual(set(baseline), {"source_head", "source_index", "base", "init_revision"})

    def test_early_profile_failure_does_not_claim_model_started(self):
        with tempfile.TemporaryDirectory() as temp:
            report, capture_mock, _pointer, _config, _paths, _calls, guard_calls = self._mocked_run(
                Path(temp), profile_error=EvidenceError("mcp_profile_rejected"))
            capture_mock.assert_not_called()
            self.assertFalse(report["mechanical_scenario_passed"])
            self.assertFalse(report["model_started"])
            self.assertFalse(report["preservation_checks"]["final_state_checked"])
            self.assertFalse(report["preservation_checks"]["provider_settings_checked"])
            self.assertFalse(report["preservation_checks"]["host_config_checked"])
            self.assertEqual(guard_calls, {"final_state": 0, "settings": 1, "host": 1})

    def test_failed_turn_scans_context_before_timeout_or_malformed_json_classification(self):
        token = "synthetic-private-context-marker"
        cases = [
            (b'{"type":"assistant"}\n' + token.encode() + b"\n", b"", "malformed-prefix"),
            (b"not-json\n", ('{"diagnostic":"' + token.replace("s", r"\u0073", 1) + '"}\n').encode(), "escaped-stderr"),
        ]
        for raw, stderr, _label in cases:
            with self.subTest(case=_label), tempfile.TemporaryDirectory() as temp:
                failure = TurnFailure("provider_turn_timeout", raw, stderr, started=True)
                report, capture_mock, *_rest = self._mocked_run(Path(temp), capture=failure)
                capture_mock.assert_called_once()
                self.assertEqual(report["evidence"], "context_token_disclosed")
                self.assertTrue(report["model_started"])

    def test_changed_provider_settings_or_host_config_fail_closed(self):
        for kwargs, expected in (
            ({"settings": [{"before": True}, {"after": True}, {"after": True}]}, "provider_settings_changed"),
            ({"host": [("missing",), ("changed",), ("changed",)]}, "doppler_host_config_changed"),
        ):
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as temp:
                report, *_rest = self._mocked_run(Path(temp), **kwargs)
                self.assertFalse(report["mechanical_scenario_passed"])
                self.assertEqual(report["evidence"], expected)
                if expected == "provider_settings_changed":
                    self.assertTrue(report["preservation_checks"]["provider_settings_checked"])
                    self.assertFalse(report["preservation_checks"]["provider_settings_unchanged"])
                else:
                    self.assertTrue(report["preservation_checks"]["host_config_checked"])
                    self.assertFalse(report["preservation_checks"]["host_config_unchanged"])

    def test_failed_started_turn_independently_runs_final_guards(self):
        with tempfile.TemporaryDirectory() as temp:
            report, capture_mock, _pointer, _config, _paths, _calls, guard_calls = self._mocked_run(
                Path(temp), capture=TurnFailure("provider_turn_failed", b"not-json\n", b"", started=True))
            capture_mock.assert_called_once()
            self.assertTrue(report["model_started"])
            checks = report["preservation_checks"]
            self.assertTrue(checks["final_state_checked"])
            self.assertTrue(checks["source_unchanged"] and checks["source_index_unchanged"])
            self.assertTrue(checks["mainline_unchanged"] and checks["no_feature_publication"])
            self.assertTrue(checks["provider_settings_checked"] and checks["provider_settings_unchanged"])
            self.assertTrue(checks["host_config_checked"] and checks["host_config_unchanged"])
            self.assertEqual(guard_calls, {"final_state": 1, "settings": 2, "host": 2})

    def test_unavailable_failure_guards_are_not_fabricated_as_verified(self):
        with tempfile.TemporaryDirectory() as temp:
            report, _capture, _pointer, _config, _paths, _calls, guard_calls = self._mocked_run(
                Path(temp), capture=TurnFailure("provider_turn_timeout", b"", b"", started=True),
                settings=[{"settings": "same"}, RuntimeError("private" )],
                host=[("missing",), RuntimeError("private")],
                final_state_error=RuntimeError("private"))
            self.assertTrue(report["model_started"])
            checks = report["preservation_checks"]
            self.assertFalse(checks["final_state_checked"])
            self.assertFalse(checks["source_unchanged"] or checks["source_index_unchanged"] or
                             checks["mainline_unchanged"] or checks["no_feature_publication"])
            self.assertFalse(checks["provider_settings_checked"] or checks["provider_settings_unchanged"])
            self.assertFalse(checks["host_config_checked"] or checks["host_config_unchanged"])
            self.assertEqual(guard_calls, {"final_state": 1, "settings": 2, "host": 2})

    def test_evaluator_rejects_extra_tools_missing_owner_direct_doppler_and_extra_log_phases(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths, config, calls, dp_calls = self._run_fixture(root)
            token = "synthetic-private-context-marker"
            events = self._native(root)
            raw = lambda value: ("\n".join(json.dumps(event) for event in value) + "\n").encode()
            events[0]["tools"].append("Bash")
            with self.assertRaises(EvidenceError):
                _evaluate(raw(events), b"", token, config)
            events = self._native(root)
            calls[1]["ownership"] = "unknown"
            config["agent_log"] = str(root / "agent.jsonl")
            Path(config["agent_log"]).write_text("".join(json.dumps(call) + "\n" for call in calls))
            with self.assertRaises(EvidenceError):
                _evaluate(raw(events), b"", token, config)
            calls[1]["ownership"] = "owned"
            dp_calls.append({"phase": "secrets_get", "origin": "model", "requested_keys": ["RUNTIME_TEST"]})
            Path(config["agent_log"]).write_text("".join(json.dumps(call) + "\n" for call in calls))
            Path(config["doppler_log"]).write_text("".join(json.dumps(call) + "\n" for call in dp_calls))
            with self.assertRaisesRegex(EvidenceError, "unexpected_direct_doppler_call"):
                _evaluate(raw(events), b"", token, config)
            dp_calls.pop()
            calls.append({"phase": "context create", "status": "completed", "exit_code": 0})
            Path(config["agent_log"]).write_text("".join(json.dumps(call) + "\n" for call in calls))
            Path(config["doppler_log"]).write_text("".join(json.dumps(call) + "\n" for call in dp_calls))
            with self.assertRaises(EvidenceError):
                _evaluate(raw(events), b"", token, config)

    def test_timeout_with_exited_leader_kills_pipe_holding_descendant_and_bounds_stdin(self):
        with tempfile.TemporaryDirectory() as temp:
            marker = Path(temp) / "orphan-marker"
            child = "import time; time.sleep(.8); open(%r,'w').write('late')" % str(marker)
            parent = "import subprocess,sys; subprocess.Popen([sys.executable,'-c',%r])" % child
            with self.assertRaisesRegex(EvidenceError, "provider_turn_timeout"):
                capture_turn([sys.executable, "-c", parent], b"", Path.cwd(), timeout=0.2, limit=4096)
            time.sleep(0.9)
            self.assertFalse(marker.exists())

            sleeper = [sys.executable, "-c", "import time; time.sleep(3)"]
            with self.assertRaisesRegex(EvidenceError, "provider_turn_timeout"):
                capture_turn(sleeper, b"x" * 16000, Path.cwd(), timeout=0.15, limit=4096)


if __name__ == "__main__":
    unittest.main()
