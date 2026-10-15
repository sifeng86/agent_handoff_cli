"""Offline launch-profile and native MCP trace validation tests."""
import json
import os
from pathlib import Path
import tempfile
import unittest

from agent_handoff.output import result
from scripts.provider_bootstrap_smoke import EvidenceError
from scripts.provider_mcp_profile import (
    MODEL, TOOL_NAMES, claude_mcp_command, claude_mcp_trace, write_mcp_config,
)


class ProviderMcpProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.private = self.root / "private"
        self.private.mkdir(mode=0o700)
        self.private.chmod(0o700)
        self.cwd = self.root / "checkout"
        self.cwd.mkdir()
        self.public = self.cwd / "instructions.md"
        self.public.write_text("public instructions\n", encoding="utf-8")
        self.engine = self.private / "proxy"
        self.engine.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.engine.chmod(0o700)
        self.policy = self.private / "policy.json"
        self.policy.write_text(json.dumps({"version": 1, "cwd": str(self.cwd),
                                           "read_files": [str(self.public)], "list_dirs": [str(self.cwd)],
                                           "protected_paths": [str(self.private)]}), encoding="utf-8")
        self.policy.chmod(0o600)
        self.broker = self.private / "broker.json"
        self.broker.write_text(json.dumps({"version": 1, "policy_file": str(self.policy),
                                           "engine_proxy": str(self.engine), "cwd": str(self.cwd)}), encoding="utf-8")
        self.broker.chmod(0o600)
        self.config = self.private / "handoff-mcp.json"
        write_mcp_config(self.config, self.broker)
        self.claude = Path("/usr/bin/env").resolve(strict=True)

    def _usage(self):
        return {MODEL: {"inputTokens": 1, "outputTokens": 1}}

    def _verify_report(self):
        return result("secrets verify", status="not_ready", secrets_shared=False,
                      secrets_mode="doppler", destination_identity="a" * 64,
                      shared_read="unavailable", shared_read_failure="authentication_required",
                      availability={"status": "not_ready", "available_keys": [],
                                    "required_missing": ["RUNTIME_TEST"], "unsupported_keys": [],
                                    "secrets_shared": False}, convergence="unverified")

    def _tool_use(self, identity, name, arguments):
        return {"type": "tool_use", "id": identity, "name": name, "input": arguments}

    def _tool_result(self, identity, value, *, failed=False):
        if isinstance(value, dict):
            value = json.dumps(value, separators=(",", ":"))
        return {"type": "tool_result", "tool_use_id": identity,
                "content": [{"type": "text", "text": value}], "is_error": failed}

    def _events(self):
        inspect = result("inspect", inspection={"observed_revision": "a" * 40,
                                                "owner_present": True, "runtime_status": "working",
                                                "protocol_version": "1.2"})
        verify = self._verify_report()
        return [
            {"type": "system", "subtype": "init", "model": MODEL,
             "tools": sorted(TOOL_NAMES), "mcp_servers": [{"name": "handoff", "status": "connected"}]},
            {"type": "assistant", "message": {"content": [
                self._tool_use("r1", "mcp__handoff__read_file", {"path": str(self.public)})]}},
            {"type": "user", "message": {"content": [self._tool_result("r1", "canonical instructions")]}},
            {"type": "assistant", "message": {"content": [
                self._tool_use("a1", "mcp__handoff__agentctl", {"argv": ["inspect"]})]}},
            {"type": "user", "message": {"content": [self._tool_result("a1", inspect)]}},
            {"type": "assistant", "message": {"content": [
                self._tool_use("a2", "mcp__handoff__agentctl", {"argv": ["secrets", "verify"]})]}},
            {"type": "user", "message": {"content": [self._tool_result("a2", verify)]}},
            {"type": "result", "subtype": "success", "is_error": False, "modelUsage": self._usage()},
        ]

    def test_config_and_command_are_exclusive_mcp_and_exact_model(self):
        value = json.loads(self.config.read_text())
        self.assertEqual(set(value["mcpServers"]), {"handoff"})
        server = value["mcpServers"]["handoff"]
        self.assertEqual(server["command"], "/usr/bin/env")
        self.assertEqual(server["args"][:6], ["-i", "PATH=/usr/bin:/bin", "LANG=C.UTF-8",
                                               "PYTHONDONTWRITEBYTECODE=1", "HOME=" + str(self.cwd),
                                               "TMPDIR=" + str(self.cwd)])
        command = claude_mcp_command(self.claude, self.config)
        self.assertEqual(command[command.index("--model") + 1], MODEL)
        self.assertIn("--strict-mcp-config", command)
        self.assertIn("--tools", command)
        self.assertEqual(command[command.index("--tools") + 1], "")
        self.assertIn("mcp__handoff__read_file,mcp__handoff__agentctl", command)
        self.assertNotIn("--add-dir", command)
        self.assertNotIn("Bash,Read,Edit,Write", command)
        with self.assertRaises(EvidenceError):
            claude_mcp_command(self.claude, self.config, model="other-model")

    def test_terminal_tool_must_be_declared_in_native_registry(self):
        events = self._events()
        events.insert(-1, {"type": "assistant", "message": {"content": [
            self._tool_use("end", "EndConversation", {})]}})
        events.insert(-1, {"type": "user", "message": {"content": [
            self._tool_result("end", "finished")]}})
        with self.assertRaises(EvidenceError):
            claude_mcp_trace(events)
        events[0]["tools"].append("EndConversation")
        self.assertEqual(len(claude_mcp_trace(events)), 3)

    def test_profile_rejects_extra_server_and_cannot_overwrite_destination(self):
        tampered = self.private / "other.json"
        value = json.loads(self.config.read_text())
        value["mcpServers"]["other"] = value["mcpServers"]["handoff"]
        tampered.write_text(json.dumps(value)); tampered.chmod(0o600)
        with self.assertRaises(EvidenceError):
            claude_mcp_command(self.claude, tampered)
        with self.assertRaises(EvidenceError):
            write_mcp_config(self.config, self.broker)

    def test_trace_normalizes_canonical_read_and_public_status_codes(self):
        normalized = claude_mcp_trace(self._events())
        self.assertEqual([item["item"]["command"] for item in normalized], [
            "cat " + str(self.public), "agentctl inspect", "agentctl secrets verify",
        ])
        self.assertEqual([item["item"]["exit_code"] for item in normalized], [0, 0, 6])

    def test_trace_rejects_unapproved_registry_model_and_uncorrelated_ids(self):
        events = self._events()
        events[0]["tools"].append("Bash")
        with self.assertRaises(EvidenceError):
            claude_mcp_trace(events)
        events = self._events()
        events[0]["model"] = "other-model"
        with self.assertRaises(EvidenceError):
            claude_mcp_trace(events)
        events = self._events()
        events[1]["message"]["content"][0]["id"] = "missing"
        with self.assertRaises(EvidenceError):
            claude_mcp_trace(events)

    def test_optional_canonical_usage_identity_must_match_requested_model(self):
        events = self._events()
        events[-1]["modelUsage"][MODEL]["canonicalModel"] = MODEL
        self.assertEqual(len(claude_mcp_trace(events)), 3)
        events[-1]["modelUsage"][MODEL]["canonicalModel"] = "claude-sonnet-other"
        with self.assertRaises(EvidenceError):
            claude_mcp_trace(events)
        events[-1]["modelUsage"][MODEL]["canonicalModel"] = None
        with self.assertRaises(EvidenceError):
            claude_mcp_trace(events)

    def test_trace_rejects_wrong_command_result_nontext_and_reordered_result(self):
        events = self._events()
        wrong = result("status", inspection={"observed_revision": "a" * 40,
                                               "owner_present": True, "runtime_status": "working",
                                               "protocol_version": "1.2"})
        events[4]["message"]["content"] = [self._tool_result("a1", wrong)]
        with self.assertRaises(EvidenceError):
            claude_mcp_trace(events)
        events = self._events()
        events[2]["message"]["content"][0]["content"] = [{"type": "image", "data": "x"}]
        with self.assertRaises(EvidenceError):
            claude_mcp_trace(events)
        events = self._events()
        events[2]["message"]["content"][0]["tool_use_id"] = "uncorrelated"
        with self.assertRaises(EvidenceError):
            claude_mcp_trace(events)

    def test_failed_tool_output_is_preserved_for_upstream_scanning(self):
        events = self._events()
        marker = "SYNTHETIC_PRIVATE_RESULT_MARKER"
        events[2]["message"]["content"] = [self._tool_result("r1", marker, failed=True)]
        normalized = claude_mcp_trace(events)
        self.assertEqual(normalized[0]["item"]["exit_code"], 1)
        self.assertEqual(normalized[0]["item"]["aggregated_output"], marker)
        events = self._events()
        events[2]["message"]["content"][0]["is_error"] = True
        events[2]["message"]["content"][0]["content"] = [{"type": "image", "data": "synthetic"}]
        with self.assertRaises(EvidenceError):
            claude_mcp_trace(events)

    def test_trace_uses_lexical_paths_without_filesystem_authority(self):
        events = self._events()
        absent = self.root / "fixture-removed-after-capture.md"
        events[1]["message"]["content"][0]["input"]["path"] = str(absent)
        normalized = claude_mcp_trace(events)
        self.assertEqual(normalized[0]["item"]["command"], "cat " + str(absent))
        invalid = self._events()
        invalid[1]["message"]["content"][0]["input"]["path"] = str(absent.parent / ".." / "private")
        with self.assertRaises(EvidenceError):
            claude_mcp_trace(invalid)

    def test_trace_accepts_observed_telemetry_and_dynamic_server_source(self):
        events = self._events()
        events[0]["mcp_servers"][0]["source"] = "dynamic"
        events.insert(1, {"type": "system", "subtype": "thinking_tokens", "count": 2})
        events.insert(2, {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed"}})
        self.assertEqual(len(claude_mcp_trace(events)), 3)
        events[1]["subtype"] = "unknown"
        with self.assertRaises(EvidenceError):
            claude_mcp_trace(events)
        events = self._events()
        events[0]["mcp_servers"][0]["source"] = "other"
        with self.assertRaises(EvidenceError):
            claude_mcp_trace(events)

    def test_relative_read_failure_is_retained_but_never_normalized_as_success(self):
        events = self._events()
        events[1]["message"]["content"][0]["input"]["path"] = ".agent/instructions.md"
        events[2]["message"]["content"][0]["content"] = [{"type": "text", "text": "invalid request"}]
        events[2]["message"]["content"][0]["is_error"] = True
        normalized = claude_mcp_trace(events)
        self.assertEqual(normalized[0]["item"]["command"], "cat .agent/instructions.md")
        self.assertEqual(normalized[0]["item"]["exit_code"], 1)
        self.assertEqual(normalized[0]["item"]["aggregated_output"], "invalid request")
        events[2]["message"]["content"][0]["is_error"] = False
        with self.assertRaises(EvidenceError):
            claude_mcp_trace(events)

    def test_telemetry_cannot_hide_tool_messages_or_precede_initialization(self):
        events = self._events()
        events.insert(0, {"type": "rate_limit_event"})
        with self.assertRaises(EvidenceError):
            claude_mcp_trace(events)
        for kind in ("rate_limit_event", "system"):
            events = self._events()
            events.insert(1, {"type": kind, "subtype": "thinking_tokens",
                              "message": {"content": [{"type": "tool_use", "name": "Bash"}]}})
            with self.subTest(kind=kind), self.assertRaises(EvidenceError):
                claude_mcp_trace(events)


if __name__ == "__main__":
    unittest.main()
