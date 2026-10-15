"""Offline verifier and real local-CLI tests for the A31 interaction proxy."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from agent_handoff.authority import Authority
from agent_handoff.git_backend import GitBackend, git_environment
from agent_handoff.publication import Runtime
from scripts.provider_bootstrap_smoke import EvidenceError, digest, read_context, write_private
from scripts.provider_interaction_smoke import (
    make_proxy, parse_command, parse_log, proxy_main, set_user_approval, verify_first_turn,
    verify_second_turn,
)
from test_publication import RepositoryFixture


class InteractionProxyVerifierTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "source"
        self.managed = self.root / "private/repos/managed"
        self.source.mkdir()
        self.managed.mkdir(parents=True)
        self.token = "e" * 64

    def test_command_parser_accepts_options_before_or_after_and_rejects_ambiguity(self):
        before = parse_command(["--authority", "/tmp/remote.git", "--checkout", "/tmp/source",
                                "--private-root", "/tmp/private", "inspect", "--publication-ref", "refs/heads/pub"])
        after = parse_command(["inspect", "--authority=/tmp/remote.git", "--checkout=/tmp/source",
                               "--private-root=/tmp/private", "--publication-ref=refs/heads/pub"])
        self.assertEqual(before, after)
        self.assertEqual(parse_command(["resume", "--help"])[0], "help")
        self.assertEqual(parse_command(["context", "--help"])[0], "help")
        self.assertEqual(parse_command(["decision", "--help"])[0], "help")
        self.assertIsNone(parse_command(["unknown", "--help"])[0])
        self.assertIsNone(parse_command(["context", "unknown", "--help"])[0])
        self.assertIsNone(parse_command(["inspect", "--authority", "a", "--authority", "b"])[0])

    def test_first_turn_verifier_rejects_model_self_report_and_predecision_mutation(self):
        with self.assertRaisesRegex(EvidenceError, "first_turn_required_phases_invalid"):
            verify_first_turn([], [], self.source, self.token, [{"type": "result", "result": "I inspected"}])
        entries = [
            {"phase": "inspect", "status": "completed", "exit_code": 0},
            {"phase": "context create", "status": "completed", "exit_code": 0},
            {"phase": "resume", "status": "decision_required", "exit_code": 3, "decision_id": "a" * 32},
        ]
        events = [{"type": "item.started", "item": {"type": "file_change", "changes": [{"path": str(self.source / "app.py")} ]}}]
        with self.assertRaisesRegex(EvidenceError, "first_turn_feature_mutation_attempt"):
            verify_first_turn(events, entries, self.source, self.token)

    def test_first_turn_accepts_repeated_readonly_discovery_and_normalized_resume_error(self):
        decision_id = "a" * 32
        phases = ["inspect", "status", "inspect", "context create", "resume"]
        entries = [{"phase": phase, "status": "completed", "exit_code": 0} for phase in phases[:-1]]
        entries.append({"phase": "resume", "status": "decision_required", "exit_code": 3,
                        "decision_id": decision_id})
        canonical = (Path(__file__).resolve().parents[1] / "template/.agent/instructions.md").read_text().strip()
        events = [{"type": "item.completed", "item": {"type": "command_execution",
                   "command": "cat .agent/instructions.md", "exit_code": 0,
                   "aggregated_output": canonical}}]
        events.extend({"type": "item.completed", "item": {"type": "command_execution",
                       "command": "agentctl " + phase, "exit_code": 1 if phase == "resume" else 0}}
                      for phase in phases)
        proof = verify_first_turn(events, entries, self.source, self.token)
        self.assertEqual(proof["decision_id"], decision_id)
        self.assertTrue(proof["readonly_discovery_before_decision"])
        events[-1]["item"].update(command='agentctl resume 2>&1; echo "exit=$?"', exit_code=0)
        masked = verify_first_turn(events, entries, self.source, self.token)
        self.assertEqual(masked["decision_id"], decision_id)
        with self.assertRaisesRegex(EvidenceError, "first_turn_decision_order_invalid"):
            verify_first_turn(events, entries[:-1] + [dict(entries[-1], exit_code=0)],
                              self.source, self.token)

    def test_readonly_help_pipeline_and_stderr_merge_preserve_cli_correlation(self):
        from scripts.provider_interaction_smoke import _trace_phases
        self.assertEqual(_trace_phases("which agentctl; agentctl --help 2>&1 | head -60"), ["help"])
        self.assertEqual(_trace_phases("agentctl status 2>&1; agentctl inspect 2>&1"),
                         ["status", "inspect"])
        self.assertEqual(_trace_phases("agentctl resume 2>&1"), ["resume"])
        for command in ("agentctl --help | tee output", "agentctl inspect > output",
                        "agentctl resume; touch output", "echo safe\ntouch output",
                        "echo safe # comment\ntouch output"):
            with self.subTest(command=command):
                self.assertIsNone(_trace_phases(command))

    def test_bounded_metadata_read_loop_accepts_only_reviewed_readonly_body(self):
        from scripts.provider_interaction_smoke import _trace_phases
        loop = 'for f in .agent/*; do [ -f "$f" ] && echo "== $f" && head -c 3000 "$f"; done'
        self.assertEqual(_trace_phases('ls -la .agent; ' + loop + '; agentctl status 2>&1 | head -80'),
                         ["status"])
        for unsafe in (loop.replace('head -c 3000', 'rm -f'),
                       loop.replace('.agent/*', '$(touch output)'),
                       loop.replace('head -c 3000 "$f"', 'head -c 3000 "$f" > output')):
            with self.subTest(command=unsafe):
                self.assertIsNone(_trace_phases(unsafe))

    def test_readonly_trace_allows_only_literal_scalar_path_assignments(self):
        from scripts.provider_interaction_smoke import _trace_phases
        self.assertEqual(_trace_phases("S=$PWD/source; A=$PWD/remote.git; agentctl status"), ["status"])
        self.assertEqual(_trace_phases("S=source; A=remote.git"), [])
        for command in ("S=$(pwd)", "S=`pwd`", "S=$HOME/source", "S=$PWD/source agentctl inspect",
                        "PATH=/tmp/bin; agentctl status", "A=$PWD/source; touch output"):
            with self.subTest(command=command):
                self.assertIsNone(_trace_phases(command))

    def test_second_turn_verifier_rejects_wrong_choice_or_edit_before_ready(self):
        entries = [
            {"phase": "decision apply", "status": "completed", "exit_code": 0,
             "decision_id": "a" * 32, "option_id": "takeover"},
            {"phase": "resume", "status": "ready", "exit_code": 0, "ownership": "owned"},
        ]
        events = [
            {"type": "item.started", "item": {"type": "file_change", "changes": [{"path": str(self.managed / "app.py")}]}},
            {"type": "item.completed", "item": {"type": "command_execution", "command": "agentctl resume", "exit_code": 0}},
        ]
        with self.assertRaisesRegex(EvidenceError, "feature_edit_before_ready_owned"):
            verify_second_turn(events, entries, self.managed, self.token, "a" * 32)
        with self.assertRaisesRegex(EvidenceError, "second_turn_choice_not_applied"):
            verify_second_turn([], [dict(entries[0], option_id="abort"), entries[1]],
                               self.managed, self.token, "a" * 32)

    def test_second_turn_allows_python_tests_only_after_ready_and_rejects_early_write(self):
        entries = [
            {"phase": "decision apply", "status": "completed", "exit_code": 0,
             "decision_id": "a" * 32, "option_id": "takeover"},
            {"phase": "resume", "status": "ready", "exit_code": 0, "ownership": "owned"},
        ]
        events = [
            {"type": "item.completed", "item": {"type": "command_execution",
             "command": "agentctl decision apply", "exit_code": 0}},
            {"type": "item.completed", "item": {"type": "command_execution",
             "command": "agentctl resume", "exit_code": 0}},
            {"type": "item.completed", "item": {"type": "command_execution",
             "command": "python3 -m unittest tests.test_app", "exit_code": 0}},
            {"type": "item.completed", "item": {"type": "command_execution",
             "command": "python3 -c 'from app import answer; assert answer() == 42'", "exit_code": 0}},
            {"type": "item.started", "item": {"type": "file_change",
             "changes": [{"path": str(self.managed / "app.py")}]}},
        ]
        proof = verify_second_turn(events, entries, self.managed, self.token, "a" * 32)
        self.assertTrue(proof["ready_owned_before_feature_change"])
        for command, code in (("python3 -I -c 'import app; print(app.answer())'", 1),
                              ("python3 -c \"import sys; sys.path.insert(0, '.'); import app; print(app.answer())\"", 0)):
            extra = {"type": "item.completed", "item": {"type": "command_execution",
                     "command": command, "exit_code": code}}
            self.assertTrue(verify_second_turn(events + [extra], entries, self.managed, self.token,
                                              "a" * 32)["ready_owned_before_feature_change"])
        premature = [events[0], events[2], events[1], events[3]]
        with self.assertRaisesRegex(EvidenceError, "second_turn_test_before_ready_owned"):
            verify_second_turn(premature, entries, self.managed, self.token, "a" * 32)
        mixed = [events[0], dict(events[1], item=dict(events[1]["item"],
                 command="agentctl resume && python3 -m unittest tests.test_app")), events[2], events[3]]
        with self.assertRaisesRegex(EvidenceError, "second_turn_test_before_ready_owned"):
            verify_second_turn(mixed, entries, self.managed, self.token, "a" * 32)
        with self.assertRaisesRegex(EvidenceError, "second_turn_shell_write_unverified"):
            verify_second_turn([events[0], dict(events[2], item=dict(events[2]["item"], command="python3 -c 'open(\"x\", \"w\")'")),
                                events[1], events[3]], entries, self.managed, self.token, "a" * 32)

    def test_claude_evidence_rejects_unsuccessful_or_wrong_model_result(self):
        from scripts.provider_bootstrap_smoke import claude_trace
        for event in (
            [{"type": "result", "is_error": True, "subtype": "error", "modelUsage": {"claude-haiku-5-5": {}}}],
            [{"type": "result", "is_error": False, "subtype": "success", "modelUsage": {"other-model": {}}}],
        ):
            with self.subTest(event=event), self.assertRaises(EvidenceError):
                claude_trace(event, "claude-haiku-5-5")


class InteractionProxyLocalCliTests(RepositoryFixture):
    def setUp(self):
        super().setUp()
        self.initial_revision = self.observe()
        self.runtime.acquire()
        self.old_session = self.runtime.sessions.current()["session_id"]
        self.owner_revision = self.observe()
        self.source = self.root / "actor-source"
        self.actor_private = self.root / "actor-private"
        self.actor_private.mkdir(mode=0o700)
        subprocess.run(["git", "clone", "--quiet", "--no-checkout", "--branch", "main", "--",
                        str(self.remote), str(self.source)], env=git_environment(), check=True,
                       capture_output=True, timeout=30)
        self.git(self.source, "fetch", "--quiet", "--no-tags", "--", str(self.remote), self.authority.publication_ref)
        self.git(self.source, "checkout", "--quiet", "--detach", self.initial_revision)
        self.source_index = (self.source / ".git/index").read_bytes()
        with Runtime(self.source, self.actor_private, self.authority) as actor:
            actor.initialize()
        self.bin_dir = self.root / "proxy-bin"
        self.bin_dir.mkdir(mode=0o700)
        self.config_path = self.root / "proxy-config.json"
        self.log_path = self.root / "proxy.jsonl"
        write_private(self.log_path, b"")
        self.python_main = "import sys; from agent_handoff.cli import main; raise SystemExit(main(sys.argv[1:]))"
        self.config = {
            "real_cli": [sys.executable, "-c", self.python_main],
            "authority": str(self.remote), "ref": self.authority.publication_ref,
            "private_root": str(self.actor_private), "source": str(self.source), "managed": None,
            "log": str(self.log_path), "verify_store": str(self.root / "proxy-verify.git"),
            "stage": "discovery", "inspection_succeeded": False, "context_created": False,
            "approved": False, "decision_id": None, "prior_owner_revision": self.owner_revision,
            "prior_owner_session": self.old_session,
        }
        write_private(self.config_path, json.dumps(self.config).encode())
        self.proxy = self.bin_dir / "agentctl"
        make_proxy(self.proxy, self.config_path)
        self.env = git_environment()
        self.env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        self.env["PATH"] = str(self.bin_dir) + os.pathsep + self.env.get("PATH", "")

    def call(self, arguments):
        return subprocess.run([str(self.proxy), *arguments], cwd=self.source, env=self.env,
                              capture_output=True, timeout=40)

    def common(self):
        return ["--authority", str(self.remote), "--publication-ref", self.authority.publication_ref,
                "--checkout", str(self.source), "--private-root", str(self.actor_private)]

    def test_user_approved_takeover_uses_real_cli_and_proves_new_session(self):
        # This is an offline runtime/CLI fixture, not evidence of an Agent interaction.
        help_result = self.call(["resume", "--help"])
        self.assertEqual(help_result.returncode, 0)
        inspected = self.call([*self.common(), "inspect"])
        self.assertEqual(inspected.returncode, 0)
        context = self.call(["context", "create", "--checkout", str(self.source), "--private-root", str(self.actor_private)])
        self.assertEqual(context.returncode, 0)
        context_path = str(self.actor_private / "context.json")
        resumed = self.call(["resume", *self.common(), "--context-file", context_path])
        self.assertEqual(resumed.returncode, 3)
        proposal = json.loads(resumed.stdout)
        self.assertEqual(proposal["status"], "decision_required")
        self.assertEqual(proposal["decision"]["category"], "takeover")
        decision_id = proposal["decision"]["decision_id"]
        self.assertEqual(self.observe(), self.owner_revision)

        premature = self.call(["decision", "apply", *self.common(), "--context-file", context_path,
                               "--decision-id", decision_id, "--option-id", "takeover"])
        self.assertEqual(premature.returncode, 7)
        self.assertEqual(self.observe(), self.owner_revision)
        with self.assertRaisesRegex(EvidenceError, "user_choice_did_not_match_pending_decision"):
            set_user_approval(self.config_path, "f" * 32, True)

        set_user_approval(self.config_path, decision_id, True)
        rediscovered = self.call([*self.common(), "inspect"])
        self.assertEqual(rediscovered.returncode, 0)
        wrong_id = self.call(["decision", "apply", *self.common(), "--context-file", context_path,
                              "--decision-id", "f" * 32, "--option-id", "takeover"])
        self.assertEqual(wrong_id.returncode, 7)
        wrong_context = self.call(["decision", "apply", *self.common(), "--context-file", str(self.root / "wrong-context.json"),
                                   "--decision-id", decision_id, "--option-id", "takeover"])
        self.assertEqual(wrong_context.returncode, 7)
        wrong_choice = self.call(["decision", "apply", *self.common(), "--context-file", context_path,
                                  "--decision-id", decision_id, "--option-id", "abort"])
        self.assertEqual(wrong_choice.returncode, 7)
        self.assertEqual(self.observe(), self.owner_revision)
        applied = self.call(["--decision-id", decision_id, "--option-id", "takeover", "--context-file", context_path,
                             *self.common(), "decision", "apply"])
        self.assertEqual(applied.returncode, 0)
        applied_result = json.loads(applied.stdout)
        self.assertIn(applied_result["command"], ("takeover", "decision apply"))
        self.assertNotEqual(self.observe(), self.owner_revision)
        post_choice_status = self.call([*self.common(), "status"])
        self.assertEqual(post_choice_status.returncode, 0)
        resumed = self.call([*self.common(), "--context-file", context_path, "resume"])
        self.assertEqual(resumed.returncode, 0)
        ready = json.loads(resumed.stdout)
        self.assertEqual(ready["status"], "ready")
        managed = Path(ready["managed_checkout"])
        managed_status = self.call(["--authority", str(self.remote), "--publication-ref",
                                    self.authority.publication_ref, "--checkout", str(managed),
                                    "--private-root", str(self.actor_private), "status"])
        self.assertEqual(managed_status.returncode, 0)
        managed_status_result = json.loads(managed_status.stdout)
        self.assertEqual(managed_status_result["status"], "completed")
        self.assertTrue(managed_status_result["inspection"]["owner_present"])
        token = read_context(self.actor_private, self.source)
        current = GitBackend(self.root / "proof.git", str(self.remote), self.authority.publication_ref)
        revision = current.observe()
        owner = current.records(revision)["owner"]["owner"]
        self.assertNotEqual(owner["session_id"], self.old_session)
        self.assertEqual(owner["session_id"], self.config_from_file()["actor_session"])
        self.assertEqual(self.observe(), revision)
        self.assertEqual(self.git(self.source, "rev-parse", "HEAD"), self.initial_revision)
        self.assertEqual((self.source / ".git/index").read_bytes(), self.source_index)
        proof = Runtime(managed, self.actor_private, self.authority, token)
        try:
            proven = proof.sessions.prove(proof.backend, revision, current.records(revision))
            self.assertEqual(proven["session_id"], owner["session_id"])
        finally:
            proof.close()
        entries = parse_log(self.log_path)
        successful_entries = [entry for entry in entries if entry.get("status") != "denied"]
        self.assertEqual([e["phase"] for e in successful_entries],
                         ["help", "inspect", "context create", "resume", "inspect", "decision apply", "status", "resume", "status"])
        applied_entry = next(entry for entry in successful_entries if entry["phase"] == "decision apply")
        self.assertEqual(applied_entry["decision_id"], decision_id)
        self.assertEqual(applied_entry["option_id"], "takeover")

    def config_from_file(self):
        return json.loads(self.config_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
