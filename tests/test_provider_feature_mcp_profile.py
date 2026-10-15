"""Offline feature MCP profile and native trace validation."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest

from agent_handoff.authority import Authority
from agent_handoff.output import result
from agent_handoff.paths import PrivateStore
from scripts.provider_bootstrap_smoke import EvidenceError
from scripts import provider_mcp_profile as auth_profile
from scripts.provider_feature_broker import FeatureBroker
from scripts.provider_feature_mcp_profile import (
    MODEL, TOOL_NAMES, claude_feature_command, claude_feature_trace, write_feature_mcp_config,
)


class ProviderFeatureMcpProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.project_private = self.root / "project-private"
        self.context = "f" * 64
        with PrivateStore(self.project_private, self.source) as store:
            store.write("context.json", {"schema_version": 1, "context": self.context})
        (self.project_private / "repos").mkdir()
        self.docs = {
            "AGENTS.md": "Instructions.\n", "CLAUDE.md": "Follow AGENTS.\n",
            ".agent/instructions.md": "Canonical protocol.\n",
            ".agent/secrets.example.yaml": "keys: {}\n",
        }
        for relative, value in self.docs.items():
            path = self.source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(value, encoding="utf-8")
        self.authority_dir = self.root / "remote.git"
        self.authority_dir.mkdir()
        self.authority = Authority(str(self.authority_dir), "refs/heads/publication")
        self.private = self.root / "broker-private"
        self.private.mkdir(mode=0o700)
        self.private.chmod(0o700)
        self.policy = self.private / "policy.json"
        self._private_json(self.policy, {"version": 1, "cwd": str(self.source),
            "read_files": [str(self.source / item) for item in self.docs], "list_dirs": [],
            "protected_paths": [str(self.project_private / "context.json"), str(self.private)]})
        self.engine = self.private / "engine-proxy.py"
        self.engine.write_text("raise SystemExit(0)\n", encoding="utf-8")
        self.engine.chmod(0o700)
        self.base_config = self.private / "broker.json"
        self._private_json(self.base_config, {"version": 2, "policy_file": str(self.policy),
            "engine_proxy": str(self.engine), "cwd": str(self.source),
            "managed_root": str(self.project_private / "repos"), "source_read_paths": list(self.docs)})
        self.feature_config = self.private / "feature.json"
        self._private_json(self.feature_config, {"version": 1, "broker_config": str(self.base_config),
            "authority": self.authority.destination, "publication_ref": self.authority.publication_ref,
            "private_root": str(self.project_private), "context_file": str(self.project_private / "context.json"),
            "approved_paths": ["app.py"]})
        self.mcp_config = self.private / "feature-mcp.json"
        write_feature_mcp_config(self.mcp_config, self.feature_config)
        self.claude = Path("/usr/bin/env").resolve(strict=True)
        self.public_file = self.source / ".agent/instructions.md"
        self.project_file = "app.py"

    @staticmethod
    def _private_json(path, value):
        path.write_text(json.dumps(value, separators=(",", ":")), encoding="utf-8")
        path.chmod(0o600)

    def _tool_use(self, identity, name, arguments):
        return {"type": "tool_use", "id": identity, "name": name, "input": arguments}

    def _tool_result(self, identity, value, failed=False):
        text = value if isinstance(value, str) else json.dumps(value, separators=(",", ":"))
        return {"type": "tool_result", "tool_use_id": identity,
                "content": [{"type": "text", "text": text}], "is_error": failed}

    def _events(self):
        instructions = "Canonical protocol.\n"
        read_value = {"path": "app.py", "content": "answer = 41\n",
                      "sha256": hashlib.sha256(b"answer = 41\n").hexdigest()}
        write_content = "answer = 42\n"
        write_value = {"status": "completed", "path": "app.py",
                       "sha256": hashlib.sha256(write_content.encode()).hexdigest(),
                       "operation_id": "1" * 32}
        resume = result("resume", status="ready", ownership="owned", observed_revision="a" * 40,
                        managed_checkout=str(self.project_private / "repos" / "session"),
                        preservation_handle="b" * 32)
        inspect = result("inspect", inspection={"observed_revision": "a" * 40,
            "owner_present": True, "runtime_status": "working", "protocol_version": "1.2"})
        uses = [
            ("read", "mcp__handoff__read_file", {"path": str(self.public_file)}, instructions),
            ("inspect", "mcp__handoff__agentctl", {"argv": ["inspect"]}, inspect),
            ("resume", "mcp__handoff__agentctl", {"argv": ["resume"]}, resume),
            ("project-read", "mcp__handoff__project_file", {"action": "read", "path": "app.py"}, read_value),
            ("project-write", "mcp__handoff__project_file", {"action": "write", "path": "app.py",
                "content": write_content, "expected_sha256": hashlib.sha256(b"answer = 41\n").hexdigest()}, write_value),
            ("project-resolve", "mcp__handoff__project_file", {"action": "resolve", "operation_id": "1" * 32},
                {"status": "completed", "path": "app.py", "sha256": write_value["sha256"],
                 "operation_id": "1" * 32, "outcome": "applied"}),
        ]
        events = [{"type": "system", "subtype": "init", "model": MODEL,
                   "tools": sorted(TOOL_NAMES), "mcp_servers": [{"name": "handoff", "status": "connected"}]}]
        for identity, name, arguments, value in uses:
            events.append({"type": "assistant", "message": {"content": [self._tool_use(identity, name, arguments)]}})
            events.append({"type": "user", "message": {"content": [self._tool_result(identity, value)]}})
        events.append({"type": "result", "subtype": "success", "is_error": False,
                       "modelUsage": {MODEL: {"inputTokens": 1, "outputTokens": 1,
                                              "canonicalModel": MODEL}}})
        return events

    def test_feature_command_is_exact_mcp_only_and_separate_from_auth_profile(self):
        command = claude_feature_command(self.claude, self.mcp_config)
        self.assertEqual(command[command.index("--model") + 1], MODEL)
        self.assertEqual(command[command.index("--allowedTools") + 1],
            "mcp__handoff__read_file,mcp__handoff__agentctl,mcp__handoff__project_file")
        self.assertIn("--strict-mcp-config", command)
        self.assertNotIn("--add-dir", command)
        self.assertEqual(set(json.loads(self.mcp_config.read_text())["mcpServers"]), {"handoff"})
        args = json.loads(self.mcp_config.read_text())["mcpServers"]["handoff"]["args"]
        self.assertIn(str(Path(__file__).resolve().parents[1] / "scripts/provider_feature_broker.py"), args)
        with self.assertRaises(EvidenceError):
            auth_profile.claude_mcp_command(self.claude, self.mcp_config)
        with self.assertRaises(EvidenceError):
            claude_feature_command(self.claude, self.mcp_config, model="other-model")
        value = json.loads(self.mcp_config.read_text())
        value["mcpServers"]["extra"] = value["mcpServers"]["handoff"]
        self._private_json(self.mcp_config, value)
        with self.assertRaises(EvidenceError):
            claude_feature_command(self.claude, self.mcp_config)

        project_schema = FeatureBroker(self.feature_config).tool_descriptions()[2]["inputSchema"]
        self.assertEqual(project_schema["type"], "object")
        self.assertEqual(len(project_schema["oneOf"]), 3)

    def test_feature_trace_preserves_correlated_native_metadata_and_exit_codes(self):
        events = self._events()
        normalized = claude_feature_trace(events)
        self.assertEqual(len(normalized), 6)
        self.assertEqual([item["item"]["exit_code"] for item in normalized], [0, 0, 0, 0, 0, 0])
        project_read = normalized[3]["item"]
        self.assertEqual(project_read["native_tool"], "mcp__handoff__project_file")
        self.assertEqual(project_read["tool_call_id"], "project-read")
        self.assertEqual(project_read["native_tool_use_event"], events[7])
        self.assertEqual(project_read["native_tool_result_event"], events[8])
        self.assertEqual(project_read["command"], "project_file read app.py")

    def test_project_file_outputs_require_matching_path_hash_and_operation_identity(self):
        cases = [
            lambda events: events[8]["message"]["content"][0].update(content=[{"type": "text", "text": json.dumps(
                {"path": "other.py", "content": "answer = 41\n", "sha256": hashlib.sha256(b"answer = 41\n").hexdigest()})}]),
            lambda events: events[10]["message"]["content"][0].update(content=[{"type": "text", "text": json.dumps(
                {"status": "completed", "path": "app.py", "sha256": "0" * 64, "operation_id": "1" * 32})}]),
            lambda events: events[12]["message"]["content"][0].update(content=[{"type": "text", "text": json.dumps(
                {"status": "completed", "path": "app.py", "sha256": "0" * 64,
                 "operation_id": "2" * 32, "outcome": "applied"})}]),
        ]
        for mutate in cases:
            events = self._events()
            mutate(events)
            with self.subTest(mutator=mutate), self.assertRaises(EvidenceError):
                claude_feature_trace(events)

    def test_feature_actions_and_native_registry_fail_closed(self):
        for argv in (["inspect", "--authority", "https://example.invalid/repo"],
                     ["checkpoint"], ["secrets", "apply"], ["inspect", "inspect"]):
            events = self._events()
            events[3]["message"]["content"][0]["input"]["argv"] = argv
            with self.subTest(argv=argv), self.assertRaises(EvidenceError):
                claude_feature_trace(events)
        for arguments in (
            {"action": "read", "path": "../secret.py"},
            {"action": "write", "path": "app.py", "content": "new", "expected_sha256": "bad"},
            {"action": "resolve", "operation_id": "../x"},
            {"action": "read", "path": "app.py", "managed_checkout": "/tmp/arbitrary"},
        ):
            events = self._events()
            events[7]["message"]["content"][0]["input"] = arguments
            with self.subTest(arguments=arguments), self.assertRaises(EvidenceError):
                claude_feature_trace(events)
        events = self._events()
        events[0]["tools"].append("Bash")
        with self.assertRaises(EvidenceError):
            claude_feature_trace(events)
        events = self._events()
        events[0]["mcp_servers"].append({"name": "other", "status": "connected"})
        with self.assertRaises(EvidenceError):
            claude_feature_trace(events)

    def test_failed_private_read_output_is_retained_and_result_order_is_checked(self):
        events = self._events()
        marker = "SYNTHETIC_PRIVATE_PATH_DENIAL"
        events[1]["message"]["content"][0]["input"]["path"] = ".env"
        events[2]["message"]["content"][0]["content"] = [{"type": "text", "text": marker}]
        events[2]["message"]["content"][0]["is_error"] = True
        normalized = claude_feature_trace(events)
        self.assertEqual(normalized[0]["item"]["exit_code"], 1)
        self.assertEqual(normalized[0]["item"]["aggregated_output"], marker)
        events = self._events()
        events[8]["message"]["content"][0]["tool_use_id"] = "not-project-read"
        with self.assertRaises(EvidenceError):
            claude_feature_trace(events)

    def test_failed_agentctl_and_project_denials_preserve_native_text(self):
        events = self._events()
        marker = "SYNTHETIC_ENGINE_DENIAL_OUTPUT"
        events[4]["message"]["content"][0]["content"] = [{"type": "text", "text": marker}]
        events[4]["message"]["content"][0]["is_error"] = True
        events[4]["message"]["content"][0]["tool_use_id"] = "inspect"
        normalized = claude_feature_trace(events)
        self.assertEqual(normalized[1]["item"]["exit_code"], 1)
        self.assertEqual(normalized[1]["item"]["aggregated_output"], marker)

        events = self._events()
        events[7]["message"]["content"][0]["input"] = {"action": "read", "path": ".env"}
        events[8]["message"]["content"][0]["content"] = [{"type": "text", "text": "project_file_denied"}]
        events[8]["message"]["content"][0]["is_error"] = True
        normalized = claude_feature_trace(events)
        denied = normalized[3]["item"]
        self.assertEqual(denied["exit_code"], 1)
        self.assertEqual(denied["aggregated_output"], "project_file_denied")
        self.assertEqual(denied["tool_arguments"], {"action": "read", "path": ".env"})

        events = self._events()
        events[7]["message"]["content"][0]["input"] = {
            "action": "read", "path": str(self.root / ".env")}
        with self.assertRaises(EvidenceError):
            claude_feature_trace(events)

    def test_unknown_project_write_and_resolve_are_exit_five(self):
        events = self._events()
        unknown_write = {"status": "unknown", "path": "app.py",
                         "sha256": hashlib.sha256(b"answer = 42\n").hexdigest(), "operation_id": "1" * 32}
        events[10]["message"]["content"] = [self._tool_result("project-write", unknown_write)]
        unknown_resolve = {"status": "unknown", "path": "app.py",
                           "sha256": hashlib.sha256(b"answer = 42\n").hexdigest(), "operation_id": "1" * 32}
        events[12]["message"]["content"] = [self._tool_result("project-resolve", unknown_resolve)]
        normalized = claude_feature_trace(events)
        self.assertEqual(normalized[4]["item"]["exit_code"], 5)
        self.assertEqual(normalized[5]["item"]["exit_code"], 5)


if __name__ == "__main__":
    unittest.main()
