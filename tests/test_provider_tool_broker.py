"""Offline security and stdio protocol tests for the restricted MCP broker."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from scripts import provider_tool_broker as broker_module
from agent_handoff.output import result


ROOT = Path(__file__).resolve().parents[1]
BROKER_SCRIPT = ROOT / "scripts" / "provider_tool_broker.py"


class ProviderToolBrokerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cwd = self.root / "work"
        self.cwd.mkdir()
        self.private = self.root / "private"
        self.private.mkdir(mode=0o700)
        self.public = self.cwd / "public.txt"
        self.public.write_text("PUBLIC_BROKER_FIXTURE\n", encoding="utf-8")
        self.secret = "SYNTHETIC_BROKER_SECRET_47fc1"
        self.context = self.private / "context.json"
        self.context.write_text(self.secret, encoding="utf-8")
        self.context.chmod(0o600)
        self.envfile = self.cwd / ".env"
        self.envfile.write_text(self.secret, encoding="utf-8")
        self.policy = self.private / "policy.json"
        self.config_file = self.private / "broker.json"
        self.engine = self.private / "agentctl-proxy"
        self.write_policy()
        self.write_engine()
        self.write_config()
        self.broker = broker_module.Broker(str(self.config_file))

    def write_policy(self):
        self.policy.write_text(json.dumps({
            "version": 1, "cwd": str(self.cwd), "read_files": [str(self.public)],
            "list_dirs": [str(self.cwd)], "protected_paths": [str(self.private)],
        }), encoding="utf-8")
        self.policy.chmod(0o600)

    def write_config(self):
        self.config_file.write_text(json.dumps({
            "version": 1, "policy_file": str(self.policy), "engine_proxy": str(self.engine),
            "cwd": str(self.cwd),
        }), encoding="utf-8")
        self.config_file.chmod(0o600)

    def write_engine(self, *, stderr="", code=0, payload=None):
        payload = payload or result("inspect", inspection={
            "observed_revision": "a" * 40, "owner_present": False,
            "runtime_status": "idle", "protocol_version": "1.2",
        })
        source = (
            "import sys\n"
            f"sys.stdout.write({json.dumps(payload, separators=(',', ':'))!r})\n"
            f"sys.stderr.write({stderr!r})\n"
            f"raise SystemExit({code})\n"
        )
        self.engine.write_text(source, encoding="utf-8")
        self.engine.chmod(0o700)

    def setup_v2(self, *, managed=None, protected_paths=None):
        agent_dir = self.cwd / ".agent"
        agent_dir.mkdir(exist_ok=True)
        docs = {
            "AGENTS.md": b"approved agent instructions\n",
            ".agent/instructions.md": b"canonical protocol\n",
            ".agent/protocol.json": b"{}\n",
            ".agent/owner.json": b"{}\n",
            ".agent/state.json": b"{}\n",
            ".agent/config.yaml": b"version: 1\n",
            ".agent/secrets.example.yaml": b"keys: {}\n",
        }
        for relative, data in docs.items():
            path = self.cwd / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        self.source_docs = docs
        self.v2_policy = self.private / "policy-v2.json"
        value = {"version": 1, "cwd": str(self.cwd),
                 "read_files": [str(self.cwd / relative) for relative in docs],
                 "list_dirs": [str(self.cwd)],
                 "protected_paths": protected_paths if protected_paths is not None else [str(self.private)]}
        self.v2_policy.write_text(json.dumps(value), encoding="utf-8")
        self.v2_policy.chmod(0o600)
        self.managed_root = self.root / "managed-checkouts"
        self.managed_root.mkdir()
        self.managed = managed or (self.managed_root / "session-a")
        self.managed.mkdir(parents=True)
        self._write_managed_docs(self.managed, docs)
        self.managed_secret = self.managed / ".env"
        self.managed_secret.write_text(self.secret, encoding="utf-8")
        self.managed_extra = self.managed / "unapproved.txt"
        self.managed_extra.write_text("UNAPPROVED_CONTENT\n", encoding="utf-8")
        self.v2_config = self.private / "broker-v2.json"
        self.v2_config.write_text(json.dumps({"version": 2, "policy_file": str(self.v2_policy),
                                              "engine_proxy": str(self.engine), "cwd": str(self.cwd),
                                              "managed_root": str(self.managed_root),
                                              "source_read_paths": list(docs)}), encoding="utf-8")
        self.v2_config.chmod(0o600)
        return broker_module.Broker(str(self.v2_config))

    @staticmethod
    def _write_managed_docs(managed, docs):
        for relative, data in docs.items():
            path = managed / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

    def write_resume_engine(self, managed=None, *, status="ready", ownership="owned"):
        managed = self.managed if managed is None else managed
        payload = result("resume", status=status, ownership=ownership,
                         observed_revision="b" * 40, managed_checkout=str(managed),
                         preservation_handle="c" * 32)
        self.write_engine(payload=payload, code=broker_module.EXIT_CODES[status])

    def test_real_landlock_read_only_approved_file_and_denied_private_paths(self):
        self.assertEqual(self.broker.read_file({"path": str(self.public)}), "PUBLIC_BROKER_FIXTURE\n")
        for path in (self.context, self.envfile, self.policy, self.config_file):
            with self.subTest(path=path.name), self.assertRaises(broker_module.BrokerError) as caught:
                self.broker.read_file({"path": str(path)})
            self.assertEqual(caught.exception.code, "read_denied")
        self.assertNotIn(self.secret, repr(self.broker.read_file({"path": str(self.public)})))

    def test_read_rejects_additional_arguments_and_noncanonical_alias(self):
        with self.assertRaises(broker_module.BrokerError):
            self.broker.read_file({"path": str(self.public), "command": "cat"})
        alias = self.cwd / "alias.txt"
        alias.symlink_to(self.public)
        with self.assertRaises(broker_module.BrokerError):
            self.broker.read_file({"path": str(alias)})

    def test_agentctl_returns_validated_structured_result_only(self):
        text = self.broker.agentctl({"argv": ["inspect", "--authority", "example", "--publication-ref", "refs/heads/test"]})
        self.assertEqual(json.loads(text)["command"], "inspect")

    def test_agentctl_rejects_unsafe_commands_arguments_and_raw_stderr(self):
        for argv in (["checkpoint"], ["secrets", "apply"], ["inspect", "--token", "x"], ["inspect", "\x00"]):
            with self.subTest(argv=argv):
                with self.assertRaises(broker_module.BrokerError) as caught:
                    self.broker.agentctl({"argv": argv})
            self.assertEqual(caught.exception.code, "engine_rejected")
        self.write_engine(stderr=self.secret)
        self.assertNotIn(self.secret, str(self._agentctl_error()))

    def _agentctl_error(self):
        try:
            self.broker.agentctl({"argv": ["inspect"]})
        except broker_module.BrokerError as error:
            return error
        self.fail("expected rejected engine call")

    def test_agentctl_rejects_status_exit_mismatch_and_invalid_public_json(self):
        self.write_engine(code=6)
        with self.assertRaises(broker_module.BrokerError):
            self.broker.agentctl({"argv": ["inspect"]})
        self.write_engine(payload={"private": self.secret})
        with self.assertRaises(broker_module.BrokerError):
            self.broker.agentctl({"argv": ["inspect"]})

    def test_agentctl_rejects_valid_result_for_a_different_command(self):
        self.write_engine(payload=result("status", inspection={
            "observed_revision": "a" * 40, "owner_present": False,
            "runtime_status": "idle", "protocol_version": "1.2",
        }))
        with self.assertRaises(broker_module.BrokerError) as caught:
            self.broker.agentctl({"argv": ["inspect"]})
        self.assertEqual(caught.exception.code, "engine_rejected")

    def test_mcp_stdio_initializes_lists_and_dispatches_without_stdin_inheritance(self):
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "read_file", "arguments": {"path": str(self.public)}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "read_file", "arguments": {"path": str(self.context)}}},
        ]
        payload = b"".join((json.dumps(item) + "\n").encode() for item in requests)
        process = subprocess.run([sys.executable, str(BROKER_SCRIPT), "--config-file", str(self.config_file)],
                                 input=payload, cwd=self.cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 timeout=20, check=False)
        self.assertEqual(process.returncode, 0, process.stderr.decode(errors="replace"))
        self.assertEqual(process.stderr, b"")
        responses = [json.loads(line) for line in process.stdout.splitlines()]
        self.assertEqual([item["id"] for item in responses], [1, 2, 3, 4])
        self.assertEqual(len(responses[1]["result"]["tools"]), 2)
        self.assertIn("PUBLIC_BROKER_FIXTURE", responses[2]["result"]["content"][0]["text"])
        denied = responses[3]["result"]["content"][0]["text"]
        self.assertTrue(responses[3]["result"]["isError"])
        self.assertNotIn(self.secret, denied)

    def test_duplicate_and_oversized_frames_exit_without_tool_execution(self):
        duplicate = b'{"jsonrpc":"2.0","jsonrpc":"2.0","id":1,"method":"initialize"}\n'
        oversized = b"{" + b" " * broker_module.MAX_FRAME + b"}\n"
        for payload in (duplicate, oversized):
            process = subprocess.run([sys.executable, str(BROKER_SCRIPT), "--config-file", str(self.config_file)],
                                     input=payload, cwd=self.cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     timeout=20, check=False)
            self.assertNotEqual(process.returncode, 0)
            self.assertNotIn(self.secret.encode(), process.stdout + process.stderr)

    def test_tool_calls_require_response_then_initialized_notification(self):
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "read_file", "arguments": {"path": str(self.public)}}},
            {"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "read_file", "arguments": {"path": str(self.public)}}},
            {"jsonrpc": "2.0", "method": "notifications/unknown"},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "read_file", "arguments": {"path": str(self.public)}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "read_file", "arguments": {"path": str(self.public)}}},
        ]
        source = io.BytesIO(b"".join((json.dumps(item) + "\n").encode() for item in requests))
        sink = io.BytesIO()
        with patch.object(broker_module.Broker, "read_file", return_value="OK") as read_file:
            self.assertEqual(broker_module.serve(str(self.config_file), source, sink), 0)
        responses = [json.loads(line) for line in sink.getvalue().splitlines()]
        self.assertEqual([response["id"] for response in responses], [1, 2, 3, 4, 5])
        self.assertEqual(read_file.call_count, 1)
        for response in (responses[0], responses[2], responses[3]):
            self.assertEqual(response["error"]["message"], "invalid request")
        self.assertEqual(responses[4]["result"]["isError"], False)

    def test_tools_metadata_is_inert_and_must_be_an_object(self):
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
                "name": "read_file", "arguments": {"path": str(self.public)},
                "_meta": {"progressToken": "x", "shell": "cat /private", "policy": ["grant"]}}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
                "name": "read_file", "arguments": {"path": str(self.public)}, "_meta": []}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {
                "name": "read_file", "arguments": {"path": str(self.public)}, "extra": "shell"}},
        ]
        source = io.BytesIO(b"".join((json.dumps(item) + "\n").encode() for item in requests))
        sink = io.BytesIO()
        with patch.object(broker_module.Broker, "read_file", return_value="OK") as read_file:
            self.assertEqual(broker_module.serve(str(self.config_file), source, sink), 0)
        responses = [json.loads(line) for line in sink.getvalue().splitlines()]
        self.assertEqual(read_file.call_count, 1)
        self.assertEqual(read_file.call_args.args[0], {"path": str(self.public)})
        self.assertFalse(responses[1]["result"]["isError"])
        self.assertEqual(responses[2]["error"]["message"], "invalid request")
        self.assertEqual(responses[3]["error"]["message"], "invalid request")

    def test_config_requires_policy_cwd_match_and_bounded_canonical_path(self):
        config = json.loads(self.config_file.read_text())
        other = self.root / "other"
        other.mkdir()
        config["cwd"] = str(other)
        self.config_file.write_text(json.dumps(config)); self.config_file.chmod(0o600)
        with self.assertRaises(broker_module.BrokerError):
            broker_module.Broker(str(self.config_file))
        self.write_config()
        with self.config_file.open("ab") as stream:
            stream.write(b" " * (broker_module.MAX_FRAME + 1))
        with self.assertRaises(broker_module.BrokerError):
            broker_module.Broker(str(self.config_file))

    def test_run_deadline_kills_pipe_holding_process_group(self):
        command = [sys.executable, "-c",
                   "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c','import time;time.sleep(5)']); time.sleep(5)"]
        start = time.monotonic()
        with self.assertRaises(broker_module.BrokerError) as caught:
            broker_module._run(command, cwd=self.cwd, env=broker_module._clean_env(self.cwd), timeout=0.2)
        self.assertEqual(caught.exception.code, "tool_timeout")
        self.assertLess(time.monotonic() - start, 3)

    def test_run_output_overflow_is_fixed_and_pipe_failure_cleans_child(self):
        with self.assertRaises(broker_module.BrokerError) as caught:
            broker_module._run([sys.executable, "-c", "import sys; sys.stderr.write('SYNTHETIC_MARKER'*200)"],
                               cwd=self.cwd, env=broker_module._clean_env(self.cwd), timeout=2, output_limit=128)
        self.assertEqual(caught.exception.code, "tool_output_rejected")
        self.assertNotIn("SYNTHETIC_MARKER", str(caught.exception))

        spawned = []
        real_popen = subprocess.Popen

        class BrokenSelector:
            def register(self, *_args):
                pass
            def get_map(self):
                return {1: object()}
            def select(self, _timeout):
                raise OSError("synthetic pipe failure")
            def close(self):
                pass

        def capture_popen(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            spawned.append(process)
            return process

        with patch.object(broker_module.subprocess, "Popen", side_effect=capture_popen), \
                patch.object(broker_module.selectors, "DefaultSelector", BrokenSelector):
            with self.assertRaises(OSError):
                broker_module._run([sys.executable, "-c", "import time;time.sleep(10)"],
                                   cwd=self.cwd, env=broker_module._clean_env(self.cwd), timeout=3)
        self.assertEqual(len(spawned), 1)
        self.assertIsNotNone(spawned[0].poll())

    def test_config_rejects_unknown_fields_and_untrusted_proxy_modes(self):
        config = json.loads(self.config_file.read_text())
        config["environment"] = {"SECRET": self.secret}
        self.config_file.write_text(json.dumps(config))
        self.config_file.chmod(0o600)
        with self.assertRaises(broker_module.BrokerError):
            broker_module.Broker(str(self.config_file))

    def test_v2_resume_grants_only_approved_managed_public_mirror_files(self):
        broker = self.setup_v2()
        with self.assertRaises(broker_module.BrokerError):
            broker.read_file({"path": str(self.managed / "AGENTS.md")})
        self.write_resume_engine()
        report = json.loads(broker.agentctl({"argv": ["resume"]}))
        self.assertEqual((report["status"], report["ownership"]), ("ready", "owned"))
        self.assertEqual(broker.read_file({"path": str(self.managed / "AGENTS.md")}), self.source_docs["AGENTS.md"].decode())
        self.assertEqual(broker.read_file({"path": str(self.managed / ".agent/instructions.md")}),
                         self.source_docs[".agent/instructions.md"].decode())
        for denied in (self.managed_secret, self.managed_extra):
            with self.subTest(name=denied.name), self.assertRaises(broker_module.BrokerError):
                broker.read_file({"path": str(denied)})
        self.assertEqual(self.policy_read_files(self.v2_policy),
                         set(broker.config["source_read_files"]) | {str(self.managed / relative) for relative in self.source_docs})
        restarted = broker_module.Broker(str(self.v2_config))
        self.assertEqual(restarted.config["active_managed_files"], broker.config["active_managed_files"])
        self.assertEqual(restarted.read_file({"path": str(self.managed / "AGENTS.md")}),
                         self.source_docs["AGENTS.md"].decode())
        with self.assertRaises(broker_module.BrokerError):
            broker.read_file({"path": str(self.managed / "AGENTS.md"), "grants": [str(self.managed)]})

    @staticmethod
    def policy_read_files(path):
        return set(json.loads(path.read_text(encoding="utf-8"))["read_files"])

    def test_v2_rebuilds_managed_grants_without_accumulating_old_checkout(self):
        broker = self.setup_v2()
        self.write_resume_engine(self.managed)
        broker.agentctl({"argv": ["resume"]})
        old_files = {str(self.managed / relative) for relative in self.source_docs}
        second = self.managed_root / "session-b"
        second.mkdir()
        self._write_managed_docs(second, self.source_docs)
        self.write_resume_engine(second)
        broker.agentctl({"argv": ["resume"]})
        current = self.policy_read_files(self.v2_policy)
        self.assertTrue(old_files.isdisjoint(current))
        self.assertTrue({str(second / relative) for relative in self.source_docs} <= current)
        self.assertEqual(set(broker.config["read_files"]), current)

    def test_v2_refresh_failures_leave_policy_and_cached_grants_unchanged(self):
        cases = []
        broker = self.setup_v2()
        cases.append((self.root / "outside-checkout", "outside"))
        cases.append((self.managed_root, "root"))
        missing = self.managed_root / "missing-file-checkout"
        missing.mkdir()
        self._write_managed_docs(missing, {key: value for key, value in self.source_docs.items() if key != "AGENTS.md"})
        cases.append((missing, "missing"))
        modified = self.managed_root / "modified-instructions"
        modified.mkdir()
        docs = dict(self.source_docs, **{".agent/instructions.md": b"changed\n"})
        self._write_managed_docs(modified, docs)
        cases.append((modified, "modified"))
        for checkout, label in cases:
            with self.subTest(case=label):
                before = self.v2_policy.read_bytes()
                cached = dict(broker.config["read_files"])
                self.write_resume_engine(checkout)
                with self.assertRaises(broker_module.BrokerError) as caught:
                    broker.agentctl({"argv": ["resume"]})
                self.assertEqual(caught.exception.code, "public_grant_refresh_failed_after_resume")
                self.assertEqual(self.v2_policy.read_bytes(), before)
                self.assertEqual(broker.config["read_files"], cached)

    def test_v2_rejects_symlink_hardlink_and_protected_managed_paths(self):
        broker = self.setup_v2()
        symlink_target = self.root / "symlink-target"
        symlink_target.mkdir()
        self._write_managed_docs(symlink_target, self.source_docs)
        alias = self.managed_root / "alias"
        alias.symlink_to(symlink_target, target_is_directory=True)
        outside = alias
        self.write_resume_engine(outside)
        with self.assertRaises(broker_module.BrokerError):
            broker.agentctl({"argv": ["resume"]})

        hardlinked = self.managed / "AGENTS.md"
        extra_link = self.managed / "AGENTS-copy.md"
        os.link(hardlinked, extra_link)
        self.write_resume_engine()
        with self.assertRaises(broker_module.BrokerError):
            broker.agentctl({"argv": ["resume"]})
        extra_link.unlink()

        protected = json.loads(self.v2_policy.read_text())
        protected["protected_paths"].append(str(self.managed_root))
        self.v2_policy.write_text(json.dumps(protected), encoding="utf-8")
        self.v2_policy.chmod(0o600)
        broker = broker_module.Broker(str(self.v2_config))
        with self.assertRaises(broker_module.BrokerError):
            broker.agentctl({"argv": ["resume"]})

    def test_v2_requires_initial_fixed_source_grants_and_resume_ready_owned(self):
        broker = self.setup_v2()
        self.write_resume_engine(status="not_ready", ownership="owned")
        report = json.loads(broker.agentctl({"argv": ["resume"]}))
        self.assertEqual(report["status"], "not_ready")
        self.assertEqual(set(broker.config["read_files"]), set(broker.config["source_read_files"]))

        policy = json.loads(self.v2_policy.read_text())
        policy["read_files"].append(str(self.private / "policy.json"))
        self.v2_policy.write_text(json.dumps(policy), encoding="utf-8")
        self.v2_policy.chmod(0o600)
        with self.assertRaises(broker_module.BrokerError):
            broker_module.Broker(str(self.v2_config))

    def test_v2_atomic_policy_write_failure_does_not_update_effective_cache(self):
        broker = self.setup_v2()
        self.write_resume_engine()
        before = self.v2_policy.read_bytes()
        cached = dict(broker.config["read_files"])
        with patch.object(broker_module.os, "replace", side_effect=OSError("synthetic atomic write failure")):
            with self.assertRaises(broker_module.BrokerError) as caught:
                broker.agentctl({"argv": ["resume"]})
        self.assertEqual(caught.exception.code, "public_grant_refresh_failed_after_resume")
        self.assertEqual(self.v2_policy.read_bytes(), before)
        self.assertEqual(broker.config["read_files"], cached)

    def test_v2_restart_rejects_forged_registry_or_extra_policy_grants(self):
        broker = self.setup_v2()
        self.write_resume_engine()
        broker.agentctl({"argv": ["resume"]})
        registry = self.v2_policy.with_name(self.v2_policy.name + ".managed.json")
        original_registry = registry.read_bytes()
        value = json.loads(registry.read_text())
        value["source_paths"].append("CLAUDE.md")
        registry.write_text(json.dumps(value), encoding="utf-8")
        registry.chmod(0o600)
        with self.assertRaises(broker_module.BrokerError):
            broker_module.Broker(str(self.v2_config))
        registry.write_bytes(original_registry); registry.chmod(0o600)

        policy = json.loads(self.v2_policy.read_text())
        extra_source = self.cwd / "CLAUDE.md"
        extra_source.write_text("unapproved at initial setup\n", encoding="utf-8")
        extra_managed = self.managed / "CLAUDE.md"
        extra_managed.write_text("unapproved at initial setup\n", encoding="utf-8")
        policy["read_files"].extend([str(extra_source), str(extra_managed)])
        self.v2_policy.write_text(json.dumps(policy), encoding="utf-8")
        self.v2_policy.chmod(0o600)
        value = json.loads(original_registry)
        value["source_paths"] = sorted(value["source_paths"] + ["CLAUDE.md"])
        value["managed_paths"] = sorted(value["managed_paths"] + [str(extra_managed)])
        registry.write_text(json.dumps(value), encoding="utf-8")
        registry.chmod(0o600)
        with self.assertRaises(broker_module.BrokerError):
            broker_module.Broker(str(self.v2_config))

    def test_v2_config_source_paths_are_immutable_fixed_relative_whitelist(self):
        self.setup_v2()
        config = json.loads(self.v2_config.read_text())
        config["source_read_paths"].append("CLAUDE.md")
        self.v2_config.write_text(json.dumps(config), encoding="utf-8")
        self.v2_config.chmod(0o600)
        with self.assertRaises(broker_module.BrokerError):
            broker_module.Broker(str(self.v2_config))


if __name__ == "__main__":
    unittest.main()
