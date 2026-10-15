"""Offline validation of the A35 headless-auth harness boundaries."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from agent_handoff.git_backend import GitBackend, git_environment
from scripts.provider_auth_fixture import CONFIG, PROJECT
from scripts.provider_bootstrap_smoke import EvidenceError, read_context, write_private
from scripts.provider_auth_smoke import (
    APPROVED_KEY, _final_state, _make_wrapper, _private_path_read, _private_snapshot_value, _setup_fixture, _write_private_json,
    agentctl_proxy_main, parse_doppler_call, verify_trace,
)


FAKE_DOPPLER = """#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
Path(os.environ['A35_FAKE_DOPPLER_AUDIT']).open('a', encoding='utf-8').write(json.dumps({
  'argv': sys.argv[1:],
  'doppler_env': sorted(k for k in os.environ if k.startswith('DOPPLER_')),
}) + '\\n')
if '--version' in sys.argv:
    sys.stdout.write('Doppler CLI 3.77.0\\n')
    raise SystemExit(0)
if 'get' in sys.argv:
    sys.stderr.write('{"error":"you must provide a token"}\\n')
    raise SystemExit(1)
raise SystemExit(0)
"""


class DopplerCommandParserTests(unittest.TestCase):
    def test_private_host_snapshot_encoding_preserves_nested_bytes(self):
        raw = ("directory-root", (1, 2), (("file", ("opaque.dat", 3), b"private-value"),))
        encoded = _private_snapshot_value(raw)
        self.assertEqual(encoded[2][0][2], {"private_bytes_base64": "cHJpdmF0ZS12YWx1ZQ=="})
        self.assertEqual(_private_snapshot_value(raw), encoded)

    def test_allows_exact_readonly_scopes_and_rejects_write_or_override_flags(self):
        regular = parse_doppler_call(["secrets", "get", APPROVED_KEY, "--project", PROJECT,
            "--config", CONFIG, "--json"])
        self.assertEqual(regular["phase"], "secrets_get")
        self.assertEqual(regular["keys"], (APPROVED_KEY,))
        engine = parse_doppler_call(["--no-read-env", "--no-check-version", "--attempts", "1",
            "--timeout", "10s", "secrets", "get", "DOPPLER_PROJECT", APPROVED_KEY,
            "DOPPLER_CONFIG", "--json", "--no-exit-on-missing-secret", "--project", PROJECT,
            "--config", CONFIG])
        self.assertEqual(engine["keys"], (APPROVED_KEY,))
        self.assertEqual(engine["executed_scope"], ("DOPPLER_CONFIG", "DOPPLER_PROJECT", APPROVED_KEY))
        self.assertEqual(parse_doppler_call(["--no-read-env", "--no-check-version", "--attempts", "1",
            "--timeout", "10s", "--version"])["phase"], "version")
        for forbidden in (
            ["secrets", "get", APPROVED_KEY, "--project", PROJECT, "--config", CONFIG, "--token", "secret"],
            ["secrets", "get", APPROVED_KEY, "--project", "other", "--config", CONFIG, "--json"],
            ["secrets", "get", APPROVED_KEY, "--project", PROJECT, "--config", CONFIG, "--api-host", "host"],
            ["secrets", "get", APPROVED_KEY, APPROVED_KEY, "--project", PROJECT, "--config", CONFIG, "--json"],
            ["secrets", "set", APPROVED_KEY, "--project", PROJECT, "--config", CONFIG],
            ["login"], ["logout"], ["run", "echo", "x"],
        ):
            with self.subTest(forbidden=forbidden):
                self.assertIsNone(parse_doppler_call(forbidden))


class AuthProxyIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.root.chmod(0o700)
        repo = Path(__file__).resolve().parents[1]
        self.agentctl = self.root / "agentctl-real-fixture"
        launcher = ("#!" + sys.executable + "\nimport sys\n"
                    "sys.path.insert(0, " + repr(str(repo / "src")) + ")\n"
                    "from agent_handoff.cli import main\nraise SystemExit(main(sys.argv[1:]))\n")
        write_private(self.agentctl, launcher.encode())
        self.agentctl.chmod(0o700)
        self.audit = self.root / "fake-doppler-audit.jsonl"
        self.fake_doppler = self.root / "doppler-cli-fixture"
        write_private(self.fake_doppler, (FAKE_DOPPLER).encode())
        self.fake_doppler.chmod(0o700)
        self.paths = _setup_fixture(self.root, self.agentctl)
        self.config_path = self.root / "agent-proxy.json"
        self.agent_log = self.root / "agent-calls.jsonl"
        self.doppler_log = self.root / "doppler-calls.jsonl"
        write_private(self.agent_log, b"")
        write_private(self.doppler_log, b"")
        self.host_config = self.root / "host-doppler"
        self.host_config.mkdir(mode=0o700)
        config = {"real_cli": [str(self.agentctl)], "authority": str(self.paths["authority"].destination),
            "ref": self.paths["authority"].publication_ref, "private_root": str(self.paths["actor_private"]),
            "context_file": str(self.paths["context_file"]), "source": str(self.paths["source"]),
            "managed": None, "manifest_sha256": self.paths["manifest_sha256"],
            "destination_identity": self.paths["destination_identity"], "approved_key": APPROVED_KEY,
            "real_doppler": str(self.fake_doppler), "host_doppler_config": str(self.host_config),
            "doppler_root": str(self.root / "doppler-calls"), "wrapper_dir": str(self.paths["bin_dir"]),
            "engine_marker": "e" * 64, "agent_log": str(self.agent_log), "doppler_log": str(self.doppler_log),
            "verify_store": str(self.root / "proxy-verify.git"), "resume_seen": False}
        Path(config["doppler_root"]).mkdir(mode=0o700)
        _write_private_json(self.config_path, config)
        self.doppler_config = self.root / "doppler-proxy.json"
        _write_private_json(self.doppler_config, config)
        _make_wrapper(self.paths["bin_dir"] / "agentctl", "agentctl_proxy_main", self.config_path)
        _make_wrapper(self.paths["bin_dir"] / "doppler", "doppler_proxy_main", self.doppler_config)
        self.env = git_environment()
        self.env["PATH"] = str(self.paths["bin_dir"]) + os.pathsep + self.env.get("PATH", "")
        self.env["A35_FAKE_DOPPLER_AUDIT"] = str(self.audit)

    def agent_args(self, command, checkout=None):
        p = self.paths
        checkout = checkout or p["source"]
        args = ["--authority", str(p["authority"].destination), "--publication-ref", p["authority"].publication_ref,
            "--checkout", str(checkout), "--private-root", str(p["actor_private"]),
            "--context-file", str(p["context_file"])]
        if command[:2] == ["secrets", "verify"] or command[:2] == ["secrets", "inspect"]:
            args.extend(["--secrets-mode", "doppler", "--manifest-file",
                         str(checkout / ".agent/secrets.example.yaml"), "--doppler-project", PROJECT,
                         "--doppler-config", CONFIG])
        return [*args, *command]

    def test_real_local_cli_verify_reports_unavailable_from_fake_missing_token_cli(self):
        before = GitBackend(self.root / "before-ref.git", str(self.paths["remote"]),
                            self.paths["authority"].publication_ref).observe()
        process = subprocess.run([str(self.paths["bin_dir"] / "agentctl"), *self.agent_args(["secrets", "verify"])],
            cwd=self.paths["source"], env=self.env, capture_output=True, timeout=90)
        self.assertEqual(process.returncode, 6)
        result = json.loads(process.stdout)
        self.assertEqual(result["status"], "not_ready")
        self.assertEqual(result["shared_read"], "unavailable")
        self.assertEqual(result["shared_read_failure"], "authentication_required")
        self.assertEqual(result["availability"]["required_missing"], [])
        self.assertNotIn(b"you must provide a token", process.stdout)
        private_diagnostics = list((self.root / "doppler-calls").glob("*/doppler-private-logs/*-stderr.bin"))
        self.assertTrue(any(b"you must provide a token" in path.read_bytes() for path in private_diagnostics))
        entries = [json.loads(line) for line in self.doppler_log.read_text().splitlines()]
        self.assertTrue(any(entry.get("phase") == "version" and entry.get("origin") == "engine" for entry in entries))
        self.assertTrue(any(entry.get("phase") == "secrets_get" and entry.get("origin") == "engine" and
                            entry.get("parent_cli_phase") == "secrets verify" and
                            entry.get("no_credentials_classified") for entry in entries))
        self.assertTrue(all(entry["doppler_env"] == [] for entry in
                            [json.loads(line) for line in self.audit.read_text().splitlines()]))
        after = GitBackend(self.root / "after-ref.git", str(self.paths["remote"]),
                           self.paths["authority"].publication_ref).observe()
        self.assertEqual(after, before)
        self.assertEqual(self.paths["mainline"].observe(), self.paths["base"])

    def test_agentctl_write_and_doppler_override_are_denied_before_execution(self):
        remote = GitBackend(self.root / "denial-before.git", str(self.paths["remote"]),
                            self.paths["authority"].publication_ref).observe()
        write_attempt = subprocess.run([str(self.paths["bin_dir"] / "agentctl"), *self.agent_args(["checkpoint"])],
            cwd=self.paths["source"], env=self.env, capture_output=True, timeout=30)
        self.assertEqual(write_attempt.returncode, 7)
        bad_doppler = subprocess.run([str(self.paths["bin_dir"] / "doppler"), "secrets", "get", APPROVED_KEY,
            "--project", "wrong", "--config", CONFIG, "--json"], cwd=self.paths["source"],
            env=self.env, capture_output=True, timeout=30)
        self.assertEqual(bad_doppler.returncode, 7)
        wrong_scope_args = self.agent_args(["inspect"])
        wrong_scope_args[wrong_scope_args.index("--authority") + 1] = str(self.root / "unapproved-authority.git")
        wrong_scope = subprocess.run([str(self.paths["bin_dir"] / "agentctl"), *wrong_scope_args],
            cwd=self.paths["source"], env=self.env, capture_output=True, timeout=30)
        self.assertEqual(wrong_scope.returncode, 7)
        self.assertFalse(self.audit.exists())
        after = GitBackend(self.root / "denial-after.git", str(self.paths["remote"]),
                            self.paths["authority"].publication_ref).observe()
        self.assertEqual(after, remote)
        self.assertEqual(self.paths["mainline"].observe(), self.paths["base"])

    def test_real_local_cli_resume_then_managed_verify_preserves_source_and_proves_owner(self):
        resume = subprocess.run([str(self.paths["bin_dir"] / "agentctl"),
            *self.agent_args(["resume"])], cwd=self.paths["source"], env=self.env,
            capture_output=True, timeout=90)
        self.assertEqual(resume.returncode, 0)
        resume_result = json.loads(resume.stdout)
        self.assertEqual(resume_result["status"], "ready")
        self.assertEqual(resume_result["ownership"], "owned")
        managed = Path(resume_result["managed_checkout"]).resolve()
        self.assertTrue(managed.is_dir())
        verify = subprocess.run([str(self.paths["bin_dir"] / "agentctl"),
            *self.agent_args(["secrets", "verify"], managed)], cwd=managed, env=self.env,
            capture_output=True, timeout=90)
        self.assertEqual(verify.returncode, 6)
        verify_result = json.loads(verify.stdout)
        self.assertEqual(verify_result["status"], "not_ready")
        self.assertEqual(verify_result["shared_read"], "unavailable")
        self.assertEqual(verify_result["shared_read_failure"], "authentication_required")
        self.assertEqual(verify_result["availability"]["status"], "completed")
        self.assertEqual(verify_result["availability"]["required_missing"], [])
        config = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(config["managed"], str(managed))
        final = _final_state(self.paths, config, read_context(self.paths["actor_private"], self.paths["source"]))
        self.assertTrue(final["resume_observed"])
        self.assertTrue(final["owner_capability_proved"])
        self.assertTrue(final["source_unchanged"])
        self.assertTrue(final["source_index_unchanged"])
        self.assertTrue(final["mainline_unchanged"])
        self.assertTrue(final["no_feature_publication"])


class AuthTraceVerifierTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.root))
        self.source = self.root / "source"
        self.source.mkdir()
        self.private = self.root / "private"
        self.private.mkdir()
        self.managed = self.private / "repos" / "managed"
        (self.managed / ".agent").mkdir(parents=True)
        (self.managed / ".agent/instructions.md").write_text("public project instructions\n")
        self.token = "f" * 64
        self.config = {"private_root": str(self.private), "evidence_root": str(self.root),
                       "managed": str(self.managed), "source": str(self.source)}
        canonical = (Path(__file__).resolve().parents[1] / "template/.agent/instructions.md").read_text().strip()
        common = ("--authority /tmp/authority.git --publication-ref refs/heads/agent-x "
                  f"--checkout {self.source} --private-root {self.private} "
                  f"--context-file {self.private}/context.json")
        self.events = [
            {"type": "item.completed", "item": {"type": "command_execution", "command": "cat .agent/instructions.md",
             "exit_code": 0, "aggregated_output": canonical}},
            {"type": "item.completed", "item": {"type": "command_execution",
             "command": "agentctl inspect " + common, "exit_code": 0}},
            {"type": "item.completed", "item": {"type": "command_execution",
             "command": "agentctl secrets verify " + common + " --secrets-mode doppler --doppler-project " + PROJECT +
             " --doppler-config " + CONFIG, "exit_code": 1}},
            {"type": "item.completed", "item": {"type": "command_execution",
             "command": "doppler secrets get RUNTIME_TEST --project agent-handoff-test --config dev --json",
             "exit_code": 1}},
        ]
        self.agent_calls = [
            {"phase": "inspect", "status": "completed", "exit_code": 0},
            {"phase": "secrets verify", "status": "not_ready", "exit_code": 6,
             "shared_read": "unavailable", "availability_status": "completed", "required_missing": []},
        ]
        self.doppler_calls = [
            {"phase": "version", "origin": "engine", "parent_cli_phase": "secrets verify", "status": "completed"},
            {"phase": "secrets_get", "origin": "engine", "parent_cli_phase": "secrets verify",
             "status": "missing_auth", "no_credentials_classified": True},
            {"phase": "secrets_get", "origin": "model", "status": "missing_auth",
             "no_credentials_classified": True, "requested_keys": [APPROVED_KEY]},
        ]

    def test_verifier_correlates_cli_and_doppler_results_without_assuming_oauth_completion(self):
        required = dict(self.config, require_auth_failure_reason=True)
        with self.assertRaisesRegex(EvidenceError, "engine_authentication_required_not_proven"):
            verify_trace(self.events, self.agent_calls, self.doppler_calls, self.token, required)
        diagnosed = [dict(call) for call in self.agent_calls]
        diagnosed[-1]["shared_read_failure"] = "authentication_required"
        self.assertTrue(verify_trace(self.events, diagnosed, self.doppler_calls, self.token,
                                    required)["engine_authentication_required_observed"])
        variable_args = [dict(event, item=dict(event["item"])) for event in self.events]
        variable_args[1]["item"]["command"] = 'agentctl inspect --context-file "$CONTEXT"'
        self.assertTrue(verify_trace(variable_args, self.agent_calls, self.doppler_calls,
                                    self.token, self.config)["canonical_instructions_before_inspect"])
        result = verify_trace(self.events, self.agent_calls, self.doppler_calls, self.token, self.config)
        self.assertTrue(result["shared_secret_verify_unavailable"])
        self.assertTrue(result["direct_missing_token_diagnostic_observed"])
        self.assertFalse(result["resume_observed"])

    def test_verifier_rejects_context_leak_mutation_private_read_and_untrusted_assignment(self):
        with self.assertRaisesRegex(EvidenceError, "context_token_in_trace"):
            verify_trace(self.events + [{"output": self.token}], self.agent_calls, self.doppler_calls, self.token, self.config)
        changed = list(self.events) + [{"type": "item.file_change", "item": {"type": "file_change", "changes": []}}]
        with self.assertRaisesRegex(EvidenceError, "project_mutation_observed"):
            verify_trace(changed, self.agent_calls, self.doppler_calls, self.token, self.config)
        private_read = list(self.events) + [{"type": "item.completed", "item": {
            "type": "command_execution", "command": "cat ~/.doppler/config.yaml", "exit_code": 0}}]
        with self.assertRaisesRegex(EvidenceError, "private_credential_path_read_attempt"):
            verify_trace(private_read, self.agent_calls, self.doppler_calls, self.token, self.config)
        guessed = list(self.events) + [{"type": "item.completed", "item": {
            "type": "command_execution", "command": "RUNTIME_TEST=guess", "exit_code": 0}}]
        with self.assertRaisesRegex(EvidenceError, "credential_or_fixture_environment_assignment"):
            verify_trace(guessed, self.agent_calls, self.doppler_calls, self.token, self.config)

    def test_missing_agent_direct_diagnostic_is_explicit_for_manual_review(self):
        result = verify_trace(self.events[:3], self.agent_calls, self.doppler_calls[:2], self.token, self.config)
        self.assertFalse(result["direct_missing_token_diagnostic_observed"])

    def test_public_managed_instruction_read_allowed_but_evidence_root_glob_rejected(self):
        original = self.source / ".agent/instructions.md"
        self.assertFalse(_private_path_read("cat " + str(original), ["cat", str(original)], self.config))
        instruction = self.managed / ".agent/instructions.md"
        self.assertFalse(_private_path_read("cat " + str(instruction), ["cat", str(instruction)], self.config))
        baseline_glob = str(self.root / "*")
        with self.assertRaisesRegex(EvidenceError, "manual_review_pending_dynamic_read"):
            _private_path_read("ls " + baseline_glob, ["ls", baseline_glob], self.config)
        with self.assertRaisesRegex(EvidenceError, "manual_review_pending_dynamic_read"):
            _private_path_read('cat "$p"', ["cat", "$p"], self.config)
        baseline = str(self.root / "provider-settings-baseline.json")
        self.assertTrue(_private_path_read("cat " + baseline, ["cat", baseline], self.config))


if __name__ == "__main__":
    unittest.main()
