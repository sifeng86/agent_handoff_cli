"""Feature-file MCP dispatch tests using real local Git ownership state."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_handoff.authority import Authority
from agent_handoff.paths import PrivateStore
from agent_handoff.publication import Runtime
from scripts import provider_feature_broker as feature_module
from scripts import provider_tool_broker as transport
from test_publication import RepositoryFixture


ROOT = Path(__file__).resolve().parents[1]


class FeatureBrokerTests(unittest.TestCase):
    git = staticmethod(RepositoryFixture.git)
    new_runtime = RepositoryFixture.new_runtime
    observe = RepositoryFixture.observe

    def setUp(self):
        RepositoryFixture.setUp(self)
        self.context = "d" * 64
        self.feature_private = self.root / "feature-private"
        with PrivateStore(self.feature_private, self.checkout) as store:
            store.write("context.json", {"schema_version": 1, "context": self.context})
        self.context_file = self.feature_private / "context.json"
        self.authority = Authority(str(self.remote), "refs/heads/feature-publication")
        self._add_public_docs_and_commit()
        self.runtime = Runtime(self.checkout, self.feature_private, self.authority, self.context)
        self.addCleanup(self.runtime.close)
        self.runtime.initialize(self.feature_base, mainline_ref="refs/heads/main")
        self.source_head = self.git(self.checkout, "rev-parse", "HEAD")
        self.source_index = (self.checkout / ".git/index").read_bytes()
        self.mainline = self.git(self.remote, "rev-parse", "refs/heads/main")
        self.broker_config, self.feature_config = self._write_broker_configs()
        self.broker = feature_module.FeatureBroker(self.feature_config)

    def _add_public_docs_and_commit(self):
        docs = {
            name: (ROOT / "template" / name).read_text(encoding="utf-8")
            for name in ("AGENTS.md", "CLAUDE.md", ".agent/instructions.md", ".agent/secrets.example.yaml")
        }
        for relative, value in docs.items():
            path = self.checkout / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(value, encoding="utf-8")
        self.git(self.checkout, "add", *docs)
        self.git(self.checkout, "commit", "-qm", "add feature broker public instructions")
        self.git(self.checkout, "push", "-q", str(self.remote), "main")
        self.feature_base = self.git(self.checkout, "rev-parse", "HEAD")
        self.public_docs = docs

    @staticmethod
    def _write_private(path, value, mode=0o600):
        path.write_text(json.dumps(value, separators=(",", ":")), encoding="utf-8")
        path.chmod(mode)

    def _write_broker_configs(self):
        private = self.root / "broker-private"
        private.mkdir(mode=0o700)
        source_paths = list(self.public_docs)
        policy = private / "policy.json"
        self._write_private(policy, {"version": 1, "cwd": str(self.checkout),
            "read_files": [str(self.checkout / item) for item in source_paths],
            "list_dirs": [], "protected_paths": [str(self.context_file), str(private)]})
        engine = private / "engine-proxy.py"
        engine.write_text(
            "import sys\n"
            f"sys.path.insert(0, {str(ROOT / 'src')!r})\n"
            "from agent_handoff.cli import main\n"
            "raise SystemExit(main())\n", encoding="utf-8")
        engine.chmod(0o700)
        base_config = private / "broker.json"
        self._write_private(base_config, {"version": 2, "policy_file": str(policy),
            "engine_proxy": str(engine), "cwd": str(self.checkout),
            "managed_root": str(self.feature_private / "repos"), "source_read_paths": source_paths})
        feature_config = private / "feature.json"
        self._write_private(feature_config, {"version": 1, "broker_config": str(base_config),
            "authority": str(self.authority.destination), "publication_ref": self.authority.publication_ref,
            "private_root": str(self.feature_private), "context_file": str(self.context_file),
            "approved_paths": ["code.txt"]})
        return base_config, feature_config

    def _argv(self, command):
        return [command]

    def _discover_and_resume(self):
        self.broker.call_tool("read_file", {"path": str(self.checkout / ".agent/instructions.md")})
        with self.assertRaises(transport.BrokerError):
            self.broker.call_tool("project_file", {"action": "read", "path": "code.txt"})
        inspect = json.loads(self.broker.call_tool("agentctl", {"argv": self._argv("inspect")}))
        self.assertEqual(inspect["status"], "completed")
        status = json.loads(self.broker.call_tool("agentctl", {"argv": self._argv("status")}))
        self.assertEqual(status["status"], "completed")
        resumed = json.loads(self.broker.call_tool("agentctl", {"argv": self._argv("resume")}))
        self.assertEqual((resumed["status"], resumed["ownership"]), ("ready", "owned"))
        self.assertEqual(resumed["managed_checkout"], str(self.broker.managed_checkout))
        return Path(resumed["managed_checkout"])

    def test_actual_git_discovery_resume_feature_read_write_and_fence(self):
        managed = self._discover_and_resume()
        publication_after_resume = self.git(self.remote, "rev-parse", self.authority.publication_ref)
        self.assertEqual(len(self.broker.tool_descriptions()), 3)
        names = [item["name"] for item in self.broker.tool_descriptions()]
        self.assertEqual(names, ["read_file", "agentctl", "project_file"])
        read = json.loads(self.broker.call_tool("project_file", {"action": "read", "path": "code.txt"}))
        self.assertEqual(read["content"], "original\n")
        write = json.loads(self.broker.call_tool("project_file", {
            "action": "write", "path": "code.txt", "content": "feature edit\n",
            "expected_sha256": read["sha256"]}))
        self.assertEqual(write["status"], "completed")
        self.assertEqual((managed / "code.txt").read_text(), "feature edit\n")
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.source_head)
        self.assertEqual((self.checkout / ".git/index").read_bytes(), self.source_index)
        self.assertEqual(self.git(self.remote, "rev-parse", "refs/heads/main"), self.mainline)
        self.assertEqual(self.git(self.remote, "rev-parse", self.authority.publication_ref), publication_after_resume)
        self.assertNotEqual((managed / "context.json").exists(), True)
        for path in (self.context_file, managed / ".env", managed / "code.txt"):
            with self.subTest(denied_path=path.name), self.assertRaises(transport.BrokerError):
                self.broker.call_tool("read_file", {"path": str(path)})

        restarted = feature_module.FeatureBroker(self.feature_config)
        with self.assertRaises(transport.BrokerError):
            restarted.call_tool("project_file", {"action": "read", "path": "code.txt"})
        restarted.call_tool("read_file", {"path": str(self.checkout / ".agent/instructions.md")})
        restarted.call_tool("agentctl", {"argv": ["inspect"]})
        resumed_read = json.loads(restarted.call_tool("project_file", {"action": "read", "path": "code.txt"}))
        self.assertEqual(resumed_read["content"], "feature edit\n")
        self.assertEqual(self.git(self.remote, "rev-parse", self.authority.publication_ref), publication_after_resume)
        with Runtime(managed, self.feature_private, self.authority, self.context) as owner:
            owner.release()
        publication_after_release = self.git(self.remote, "rev-parse", self.authority.publication_ref)
        for action in (
            lambda: self.broker.call_tool("project_file", {"action": "read", "path": "code.txt"}),
            lambda: self.broker.call_tool("project_file", {"action": "write", "path": "code.txt",
                "content": "fenced\n", "expected_sha256": write["sha256"]}),
        ):
            with self.assertRaises(transport.BrokerError):
                action()
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.source_head)
        self.assertEqual((self.checkout / ".git/index").read_bytes(), self.source_index)
        self.assertEqual(self.git(self.remote, "rev-parse", "refs/heads/main"), self.mainline)
        self.assertEqual(self.git(self.remote, "rev-parse", self.authority.publication_ref), publication_after_release)

    def test_bootstrap_order_and_exact_feature_arguments(self):
        for tool, arguments in (
            ("project_file", {"action": "read", "path": "code.txt"}),
            ("agentctl", {"argv": self._argv("inspect")}),
        ):
            with self.subTest(tool=tool), self.assertRaises(transport.BrokerError):
                self.broker.call_tool(tool, arguments)
        self.broker.call_tool("read_file", {"path": str(self.checkout / ".agent/instructions.md")})
        with self.assertRaises(transport.BrokerError):
            self.broker.call_tool("agentctl", {"argv": self._argv("resume")})
        self.broker.call_tool("agentctl", {"argv": self._argv("inspect")})
        managed = self.broker.managed_checkout
        for invalid in (
            {"action": "read", "path": "../code.txt"},
            {"action": "read", "path": ".env"},
            {"action": "read", "path": "code.txt", "managed_checkout": str(self.checkout)},
            {"action": "write", "path": "code.txt", "content": "x", "expected_sha256": "0" * 64,
             "command": "arbitrary"},
        ):
            with self.subTest(arguments=invalid), self.assertRaises(transport.BrokerError):
                self.broker.call_tool("project_file", invalid)
        # Untrusted arguments cannot select checkout, context, authority, or CLI flags.
        for argv in (
            ["inspect", "--authority", "https://example.invalid/repo", "--publication-ref", "refs/heads/other"],
            ["inspect", "--checkout", str(self.checkout)],
            ["inspect", "--private-root", str(self.root)],
            ["inspect", "--context-file", str(self.context_file)],
            ["inspect", "inspect"], ["resume", "--context-file", "x"],
            ["secrets", "verify", "--checkout", str(self.checkout)],
        ):
            with self.subTest(argv=argv), self.assertRaises(transport.BrokerError):
                self.broker.call_tool("agentctl", {"argv": argv})
        with self.assertRaises(transport.BrokerError):
            self.broker.call_tool("agentctl", {"argv": ["inspect"] , "private_root": str(self.root)})
        self.assertEqual(managed, self.broker.managed_checkout)

    def test_feature_wrapper_requires_trusted_private_exact_config(self):
        original = self.feature_config.read_bytes()
        config = json.loads(original)
        for change in (
            lambda item: item.update(version=2),
            lambda item: item.update(extra="unsupported"),
            lambda item: item.update(context_file=str(self.root / "other-context.json")),
        ):
            self.feature_config.write_text(json.dumps(config), encoding="utf-8")
            self.feature_config.chmod(0o600)
            mutated = json.loads(self.feature_config.read_text())
            change(mutated)
            self.feature_config.write_text(json.dumps(mutated), encoding="utf-8")
            self.feature_config.chmod(0o600)
            with self.assertRaises(transport.BrokerError) as caught:
                feature_module.FeatureBroker(self.feature_config)
            self.assertEqual(caught.exception.code, "configuration_rejected")
        self.feature_config.write_bytes(original)
        self.feature_config.chmod(0o600)
        hardlink = self.feature_config.with_name("feature-hardlink.json")
        os.link(self.feature_config, hardlink)
        with self.assertRaises(transport.BrokerError) as caught:
            feature_module.FeatureBroker(hardlink)
        self.assertEqual(caught.exception.code, "configuration_rejected")

    def test_stdio_factory_lists_three_feature_tools_but_auth_default_stays_two(self):
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        ]
        source = io.BytesIO(b"".join((json.dumps(item) + "\n").encode() for item in requests))
        sink = io.BytesIO()
        feature = feature_module.FeatureBroker(self.feature_config)
        self.assertEqual(transport.serve(str(self.feature_config), source, sink,
                                         broker_factory=lambda _config: feature), 0)
        listed = json.loads(sink.getvalue().splitlines()[-1])["result"]["tools"]
        self.assertEqual([item["name"] for item in listed], ["read_file", "agentctl", "project_file"])
        self.assertEqual(listed[1]["inputSchema"]["properties"]["argv"]["enum"],
                         [["inspect"], ["status"], ["resume"], ["secrets", "inspect"], ["secrets", "verify"]])
        self.assertNotIn("resources", json.loads(sink.getvalue().splitlines()[-1])["result"])

        auth_source = io.BytesIO(b"".join((json.dumps(item) + "\n").encode() for item in requests))
        auth_sink = io.BytesIO()
        base = transport.serve(str(self.broker_config), auth_source, auth_sink)
        self.assertEqual(base, 0)
        base_tools = json.loads(auth_sink.getvalue().splitlines()[-1])["result"]["tools"]
        self.assertEqual([item["name"] for item in base_tools], ["read_file", "agentctl"])


if __name__ == "__main__":
    unittest.main()
