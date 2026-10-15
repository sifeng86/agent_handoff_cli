"""Offline proof checks and real local-CLI fixture for the A63 readiness harness."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from agent_handoff.git_backend import GitBackend, git_environment
from scripts.provider_bootstrap_smoke import EvidenceError, write_private
from scripts.provider_interaction_smoke import _fixture
from scripts.provider_readiness_smoke import (
    REQUIRED_KEY, _configure_fixture, _fixture_files, _make_proxy, _parse_call, digest,
    _final_state, _read_private_json, _write_json, verify_agent_trace,
)


class ReadinessVerifierTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.managed = self.root / "private/repos/managed"
        self.source.mkdir()
        self.managed.mkdir(parents=True)
        self.token = "d" * 64

    def valid_trace(self):
        canonical = (Path(__file__).resolve().parents[1] / "template/.agent/instructions.md").read_text().strip()
        events = [
            {"type": "item.completed", "item": {"type": "command_execution", "command": "cat .agent/instructions.md",
             "exit_code": 0, "aggregated_output": canonical}},
            {"type": "item.completed", "item": {"type": "command_execution", "command": "agentctl inspect",
             "exit_code": 0}},
            {"type": "item.completed", "item": {"type": "command_execution", "command": "agentctl resume",
             "exit_code": 1}},
            {"type": "item.completed", "item": {"type": "command_execution", "command": "agentctl secrets verify",
             "exit_code": 1}},
        ]
        calls = [
            {"phase": "inspect", "status": "completed", "exit_code": 0},
            {"phase": "resume", "status": "not_ready", "exit_code": 6, "ownership": "owned",
             "availability_status": "not_ready", "required_missing": [REQUIRED_KEY]},
            {"phase": "secrets verify", "status": "not_ready", "exit_code": 6,
             "availability_status": "not_ready", "required_missing": [REQUIRED_KEY]},
        ]
        return events, calls

    def test_gate_accepts_resume_only_and_masked_aggregate_tool_exit(self):
        events, calls = self.valid_trace()
        events[1] = {"type": "item.completed", "item": {"type": "command_execution",
            "command": "agentctl inspect; agentctl resume", "exit_code": 0}}
        events = events[:2]
        calls = calls[:2]
        result = verify_agent_trace(events, calls, self.source, self.managed, self.token)
        self.assertTrue(result["required_secret_missing_verified"])
        self.assertTrue(result["resume_not_ready_owned_observed"])

    def test_gate_accepts_source_secret_diagnostic_with_manifest_and_local_mode(self):
        self.assertEqual(_parse_call(["secrets", "verify", "--secrets-mode", "local-env",
                                      "--manifest-file", ".agent/secrets.example.yaml"])[0], "secrets verify")
        from scripts.provider_readiness_smoke import _command_phases
        self.assertEqual(_command_phases("cat .agent/* 2>/dev/null | head -200; cat dependent_test.py; which agentctl; agentctl --help 2>&1 | head -80"),
                         ["help"])
        with self.assertRaises(EvidenceError):
            _command_phases("cat .agent/* 2> output")

    def test_verifier_requires_real_proxy_gate_and_rejects_marker_mutation_self_report(self):
        events, calls = self.valid_trace()
        result = verify_agent_trace(events, calls, self.source, self.managed, self.token)
        self.assertTrue(result["resume_not_ready_owned_observed"])
        self.assertTrue(result["required_secret_missing_verified"])
        with self.assertRaisesRegex(EvidenceError, "proxy_trace_mismatch"):
            verify_agent_trace(events, [], self.source, self.managed, self.token)
        for assignment in ("RUNTIME_REQUIRED_A63=placeholder", "DOPPLER_TOKEN=placeholder",
                           "AGENT_A63_LAUNCH_MARKER=/tmp/other-marker"):
            assigned = events + [{"type": "item.completed", "item": {
                "type": "command_execution", "command": assignment, "exit_code": 0}}]
            with self.assertRaisesRegex(EvidenceError, "secret_or_fixture_environment_assignment_observed"):
                verify_agent_trace(assigned, calls, self.source, self.managed, self.token)
        with self.assertRaisesRegex(EvidenceError, "dependent_test_marker_created"):
            verify_agent_trace(events, calls, self.source, self.managed, self.token, marker_exists=True)
        mutated = list(events) + [{"type": "item.started", "item": {"type": "file_change",
            "changes": [{"path": str(self.managed / "dependent_test.py")} ]}}]
        with self.assertRaisesRegex(EvidenceError, "project_file_mutation_observed"):
            verify_agent_trace(mutated, calls, self.source, self.managed, self.token)

    def test_command_parser_allows_only_documented_readonly_family_help(self):
        self.assertEqual(_parse_call(["--authority", "a", "secrets", "verify", "--help"])[0], "help")
        self.assertEqual(_parse_call(["secrets", "--help"])[0], "help")
        self.assertEqual(_parse_call(["--help"])[0], "help")
        self.assertEqual(_parse_call(["decision", "apply", "--help"])[0], "help")
        self.assertEqual(_parse_call(["secrets", "configure", "--help"])[0], "help")
        self.assertEqual(_parse_call(["context", "--help"])[0], "help")
        self.assertIsNone(_parse_call(["unknown", "--help"])[0])
        self.assertIsNone(_parse_call(["secrets", "configure"])[0])


class ReadinessLocalCliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        repository = Path(__file__).resolve().parents[1]
        self.cli = self.root / "source-agentctl"
        launcher = ("#!" + sys.executable + "\nimport sys\n"
                    "sys.path.insert(0, " + repr(str(repository / "src")) + ")\n"
                    "from agent_handoff.cli import main\nraise SystemExit(main(sys.argv[1:]))\n")
        write_private(self.cli, launcher.encode())
        self.cli.chmod(0o700)
        self.paths = _fixture(self.root, self.cli, prior_owner=False, extra_files=_fixture_files())
        self.paths["root"] = self.root
        self.common = _configure_fixture(self.paths)
        self.config_path = self.root / "proxy-config.json"
        self.log_path = self.root / "proxy-calls.jsonl"
        write_private(self.log_path, b"")
        self.config = {"real_cli": self.paths["real_cli"],
            "authority": str(self.paths["authority"].destination),
            "ref": self.paths["authority"].publication_ref,
            "private_root": str(self.paths["actor_private"]),
            "context_file": str(self.paths["context_file"]),
            "source": str(self.paths["source"]), "managed": None,
            "required_key": REQUIRED_KEY, "log": str(self.log_path),
            "verify_store": str(self.root / "verify-proxy.git"),
            "stage": "discovery", "resume_seen": False,
            "initial_source_head": self.paths["source_head"],
            "initial_source_index_sha256": self.paths["source_index"],
            "mainline_head": self.paths["base"],
            "initial_publication_revision": self.paths["init_revision"]}
        self.config["manifest_sha256"] = digest((self.paths["source"] / ".agent/secrets.example.yaml").read_bytes())
        self.config["secret_destination_identity"] = self.paths["secret_destination_identity"]
        _write_json(self.config_path, self.config)
        self.proxy_dir = self.paths["bin_dir"]
        self.proxy = self.proxy_dir / "agentctl"
        _make_proxy(self.proxy, self.config_path)
        self.env = git_environment()
        self.env["PATH"] = str(self.proxy_dir) + os.pathsep + self.env.get("PATH", "")

    def call(self, phase, *, authority=None, checkout=None):
        args = ["--authority", authority or str(self.paths["authority"].destination),
                "--publication-ref", self.paths["authority"].publication_ref,
                "--checkout", str(checkout or self.paths["source"]),
                "--private-root", str(self.paths["actor_private"]),
                "--context-file", str(self.paths["context_file"]), *phase.split()]
        return subprocess.run([str(self.proxy), *args], cwd=self.paths["source"], env=self.env,
                              capture_output=True, timeout=60)

    def test_real_cli_resume_and_verify_return_not_ready_without_starting_program(self):
        source = self.paths["source"]
        remote = self.paths["remote"]
        initial = GitBackend(self.root / "test-initial.git", str(remote), self.paths["authority"].publication_ref)
        self.assertEqual(initial.observe(), self.paths["init_revision"])
        self.assertIsNone(initial.records(self.paths["init_revision"])["owner"]["owner"])
        inspected = self.call("inspect")
        self.assertEqual(inspected.returncode, 0)
        verified_source = subprocess.run([str(self.proxy), "--authority", str(self.paths["authority"].destination),
            "--publication-ref", self.paths["authority"].publication_ref, "--checkout", str(source),
            "--private-root", str(self.paths["actor_private"]), "--context-file", str(self.paths["context_file"]),
            "secrets", "verify", "--secrets-mode", "local-env", "--manifest-file",
            str(source / ".agent/secrets.example.yaml")], cwd=source, env=self.env,
            capture_output=True, timeout=60)
        self.assertEqual(verified_source.returncode, 6)
        resumed = self.call("resume")
        self.assertEqual(resumed.returncode, 6)
        resume_result = json.loads(resumed.stdout)
        self.assertEqual(resume_result["status"], "not_ready")
        self.assertEqual(resume_result["ownership"], "owned")
        self.assertEqual(resume_result["secret_availability"]["required_missing"], [REQUIRED_KEY])
        managed = Path(resume_result["managed_checkout"])
        verified = self.call("secrets verify", checkout=managed)
        self.assertEqual(verified.returncode, 6)
        verify_result = json.loads(verified.stdout)
        self.assertEqual(verify_result["status"], "not_ready")
        self.assertEqual(verify_result["availability"]["required_missing"], [REQUIRED_KEY])
        marker = self.root / "test-program.marker"
        env = dict(git_environment(), AGENT_A63_LAUNCH_MARKER=str(marker))
        process = subprocess.run([sys.executable, str(source / "dependent_test.py")], cwd=source, env=env,
                                 capture_output=True, timeout=10, umask=0o077)
        self.assertNotEqual(process.returncode, 0)
        self.assertEqual(marker.read_text(encoding="utf-8"), "started")
        marker.unlink()
        self.assertFalse(marker.exists())
        self.assertFalse((source / ".env").exists())
        self.assertFalse((managed / ".env").exists())
        self.assertTrue(self.paths["context_file"].is_file())
        config = _read_private_json(self.config_path)
        log = [json.loads(line) for line in self.log_path.read_text().splitlines() if line]
        self.assertEqual([entry["phase"] for entry in log], ["inspect", "secrets verify", "resume", "secrets verify"])
        self.assertEqual(log[1]["exit_code"], 6)
        self.assertEqual(log[1]["required_missing"], [REQUIRED_KEY])
        self.assertEqual(log[2]["exit_code"], 6)
        state = _final_state(self.paths, config, marker)
        self.assertTrue(state["owner_capability_proved"])

    def test_proxy_rejects_wrong_authority_without_publication_change(self):
        before = GitBackend(self.root / "wrong-authority-before.git", str(self.paths["remote"]),
                            self.paths["authority"].publication_ref).observe()
        rejected = self.call("inspect", authority=str(self.root / "other.git"))
        self.assertEqual(rejected.returncode, 7)
        after = GitBackend(self.root / "wrong-authority-after.git", str(self.paths["remote"]),
                           self.paths["authority"].publication_ref).observe()
        self.assertEqual(after, before)
        calls = [json.loads(line) for line in self.log_path.read_text().splitlines() if line]
        self.assertEqual(calls[-1]["status"], "denied")


if __name__ == "__main__":
    unittest.main()
