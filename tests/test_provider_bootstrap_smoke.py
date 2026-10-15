import json
import subprocess
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.provider_bootstrap_smoke import EvidenceError, make_wrapper, verify_trace


class ProviderBootstrapVerifierTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.managed = self.root / "private" / "repos" / "managed"
        self.managed.mkdir(parents=True)
        self.source.mkdir()
        self.token = "c" * 64

    def fixture(self):
        events = [
            {"type": "item.completed", "item": {"type": "command_execution", "command": "cat .agent/instructions.md", "exit_code": 0, "aggregated_output": Path("template/.agent/instructions.md").read_text()}},
            {"type": "item.completed", "item": {"type": "command_execution", "command": "agentctl inspect --authority /tmp/remote.git --publication-ref refs/heads/pub --checkout " + str(self.source) + " --private-root /tmp/private", "exit_code": 0}},
            {"type": "item.completed", "item": {"type": "command_execution", "command": "agentctl context create --checkout " + str(self.source) + " --private-root /tmp/private", "exit_code": 0}},
            {"type": "item.completed", "item": {"type": "command_execution", "command": "agentctl resume --authority /tmp/remote.git --publication-ref refs/heads/pub --checkout " + str(self.source) + " --private-root /tmp/private --context-file /tmp/private/context.json", "exit_code": 0}},
            {"type": "item.file_change", "item": {"type": "file_change", "changes": [{"path": str(self.managed / "app.py")}]}},
        ]
        calls = [
            {"command": "help", "status": "help", "exit_code": 0},
            {"command": "inspect", "status": "completed", "exit_code": 0},
            {"command": "context create", "status": "completed", "exit_code": 0},
            {"command": "resume", "status": "ready", "ownership": "owned", "exit_code": 0},
        ]
        return events, calls

    def test_valid_fresh_bootstrap_order_accepts_optional_automatic_agents_loading(self):
        events, calls = self.fixture()
        result = verify_trace(events, calls, self.source, self.managed, self.token)
        self.assertTrue(result["instructions_read_before_inspect"])
        self.assertFalse(result["agents_read_observed"])
        self.assertTrue(result["resume_ready_owned_before_feature_change"])

    def test_mutation_before_readonly_inspect_is_rejected(self):
        events, calls = self.fixture()
        events.insert(0, {"type": "item.completed", "item": {"type": "command_execution", "command": "touch app.py", "exit_code": 0}})
        with self.assertRaisesRegex(EvidenceError, "ambiguous_pre_resume_command"):
            verify_trace(events, calls, self.source, self.managed, self.token)

    def test_canonical_instruction_read_must_precede_inspect(self):
        events, calls = self.fixture()
        events[0]["item"]["command"] = "cat README.md"
        with self.assertRaisesRegex(EvidenceError, "canonical_instruction_read_unproven"):
            verify_trace(events, calls, self.source, self.managed, self.token)

    def test_model_self_report_without_wrapper_proof_is_rejected(self):
        events, _calls = self.fixture()
        with self.assertRaisesRegex(EvidenceError, "required_wrapper_phases_unproven"):
            verify_trace(events, [], self.source, self.managed, self.token)

    def test_context_token_in_jsonl_is_rejected(self):
        events, calls = self.fixture()
        events[0]["item"]["command"] += " " + self.token
        with self.assertRaisesRegex(EvidenceError, "context_token_in_provider_trace"):
            verify_trace(events, calls, self.source, self.managed, self.token)

    def test_owned_work_after_resume_is_checked_by_final_state(self):
        events, calls = self.fixture()
        events.append({"type": "item.completed", "item": {"type": "command_execution",
                      "command": "touch unexpected.py", "exit_code": 0}})
        self.assertTrue(verify_trace(events, calls, self.source, self.managed, self.token)
                        ["resume_ready_owned_before_feature_change"])

    def test_listing_canonical_filename_does_not_prove_instruction_read(self):
        events, calls = self.fixture()
        events[0]["item"].update(command="ls .agent/instructions.md", aggregated_output=".agent/instructions.md")
        with self.assertRaisesRegex(EvidenceError, "canonical_instruction_read_unproven"):
            verify_trace(events, calls, self.source, self.managed, self.token)

    def test_read_only_allowlist_rejects_embedded_writes_and_external_programs(self):
        from scripts.provider_bootstrap_smoke import read_only_command
        for command in ("sed -i.bak s/a/b/ app.py", "sed -n 'w unwanted' app.py",
                        "find . -fprint unwanted", "rg --pre=touch app.py", "git log --output=unwanted"):
            with self.subTest(command=command):
                self.assertFalse(read_only_command(command))

    def test_shell_wrapped_cli_events_preserve_bootstrap_order(self):
        import shlex
        events, calls = self.fixture()
        for event in events:
            if event["item"]["type"] == "command_execution":
                event["item"]["command"] = "/bin/bash -lc " + shlex.quote(event["item"]["command"])
        self.assertTrue(verify_trace(events, calls, self.source, self.managed, self.token)
                        ["resume_ready_owned_before_feature_change"])

    def test_started_file_change_before_resume_is_rejected(self):
        events, calls = self.fixture()
        events.insert(3, {"type": "item.started", "item": {"type": "file_change",
                          "changes": [{"path": str(self.managed / "app.py")}]}})
        with self.assertRaisesRegex(EvidenceError, "feature_mutation_order_unproven"):
            verify_trace(events, calls, self.source, self.managed, self.token)

    def test_command_help_is_discovery_without_private_context(self):
        wrapper = self.root / "agentctl"
        config = self.root / "config.json"
        log = self.root / "calls.jsonl"
        log.touch(mode=0o600)
        config.write_text(json.dumps({"real": sys.executable, "log": str(log)}))
        make_wrapper(wrapper, config)
        for command in ("inspect", "resume", "context"):
            result = subprocess.run([str(wrapper), command, "--help"], capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0)
        self.assertTrue(all(json.loads(line)["command"] == "help" for line in log.read_text().splitlines()))

    def test_duplicate_scope_option_cannot_change_executed_checkout(self):
        wrapper, config, log = self.root / "agentctl", self.root / "config.json", self.root / "calls.jsonl"
        log.touch(mode=0o600)
        config.write_text(json.dumps({"real": sys.executable, "log": str(log),
            "source": str(self.source), "private": str(self.root / "private"),
            "authority": "/tmp/remote.git", "ref": "refs/heads/pub", "managed": None}))
        make_wrapper(wrapper, config)
        result = subprocess.run([str(wrapper), "inspect", "--private-root", str(self.root / "private"),
            "--checkout", str(self.source), "--checkout", "/tmp/unapproved",
            "--authority", "/tmp/remote.git", "--publication-ref", "refs/heads/pub"],
            capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 7)
        self.assertEqual(log.read_text(), "")

    def test_global_options_before_command_are_normal_cli_syntax(self):
        from scripts.provider_bootstrap_smoke import cli_phase
        self.assertEqual(cli_phase(["--authority", "/tmp/remote.git", "--private-root", "/tmp/private", "inspect"]), "inspect")
        self.assertEqual(cli_phase(["--private-root=/tmp/private", "context", "create"]), "context create")
        self.assertEqual(cli_phase(["--private-root", "/tmp/private", "resume", "--help"]), "help")

    def test_sort_discovery_is_readonly_but_output_file_is_not(self):
        from scripts.provider_bootstrap_smoke import read_only_command
        self.assertTrue(read_only_command("pwd && rg --files | sort"))
        self.assertFalse(read_only_command("sort --output=app.py input"))

    def test_readonly_discovery_allows_error_sink_and_failed_context_usage(self):
        from scripts.provider_bootstrap_smoke import read_only_command
        self.assertTrue(read_only_command("ls .agent 2>/dev/null"))
        self.assertTrue(read_only_command("agentctl --private-root /tmp/private context"))
        self.assertFalse(read_only_command("ls .agent 2>app.py"))

    def test_remote_listing_is_readonly_but_remote_edit_is_not(self):
        from scripts.provider_bootstrap_smoke import read_only_command
        self.assertTrue(read_only_command("git remote -v"))
        self.assertFalse(read_only_command("git remote set-url origin /tmp/other"))

    def test_rejected_context_init_probe_does_not_claim_context_creation(self):
        events, calls = self.fixture()
        events.insert(2, {"type": "item.completed", "item": {"type": "command_execution",
                          "command": "agentctl context init", "exit_code": 7}})
        self.assertTrue(verify_trace(events, calls, self.source, self.managed, self.token)
                        ["resume_ready_owned_before_feature_change"])
        events[2]["item"]["exit_code"] = 0
        with self.assertRaisesRegex(EvidenceError, "context_discovery_effects_unproven"):
            verify_trace(events, calls, self.source, self.managed, self.token)

    def test_legacy_provider_constructors_fail_before_spawn(self):
        from scripts.provider_bootstrap_smoke import claude_command, codex_command
        with patch("scripts.provider_bootstrap_smoke.subprocess.run") as spawn:
            for constructor, args in (
                (codex_command, ("codex", self.source, self.root / "private", self.root / "remote",
                                 "gpt-6-luna", self.root / "codex-state")),
                (claude_command, ("claude", self.root / "private", self.root / "remote", "claude-haiku-5-5")),
            ):
                with self.subTest(provider=constructor.__name__), self.assertRaisesRegex(
                        EvidenceError, "legacy_provider_profile_not_isolated"):
                    constructor(*args)
            spawn.assert_not_called()

    def test_legacy_interaction_turn_rejects_before_files_or_provider_spawn(self):
        from scripts.provider_interaction_smoke import _run_turn
        trace = self.root / "turn.jsonl"
        stderr = self.root / "turn.stderr"
        with patch("scripts.provider_interaction_smoke.subprocess.run") as spawn:
            with self.assertRaisesRegex(EvidenceError, "legacy_provider_profile_not_isolated"):
                _run_turn("claude", "claude-haiku-5-5", self.root / "remote", self.root / "private",
                          self.source, self.root / "bin", trace, stderr, "prompt")
            spawn.assert_not_called()
        self.assertFalse(trace.exists())
        self.assertFalse(stderr.exists())

    def test_claude_trace_correlates_reads_commands_and_edits(self):
        from scripts.provider_bootstrap_smoke import claude_trace
        events = [
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "r", "name": "Read", "input": {"file_path": "/tmp/instructions.md"}}]}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "r", "content": "     1→first\n     2→second"}]}},
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "e", "name": "Edit", "input": {"file_path": str(self.managed / "app.py")}}]}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "e", "content": "updated"}]}},
            {"type": "result", "is_error": False, "subtype": "success", "modelUsage": {"claude-haiku-5-5": {}}},
        ]
        normalized = claude_trace(events, "claude-haiku-5-5")
        self.assertEqual(normalized[0]["item"]["aggregated_output"], "first\nsecond")
        self.assertEqual(normalized[1]["type"], "item.started")
        with self.assertRaisesRegex(EvidenceError, "requested_model_unproven"):
            claude_trace(events, "another-model")
        events[1]["message"]["content"][0]["tool_use_id"] = "foreign"
        with self.assertRaisesRegex(EvidenceError, "uncorrelated"):
            claude_trace(events, "claude-haiku-5-5")

    def test_claude_readonly_shell_discovery_and_stderr_forwarding(self):
        from scripts.provider_bootstrap_smoke import cli_name, read_only_command
        self.assertEqual(cli_name("agentctl inspect --authority /tmp/repo --publication-ref refs/heads/pub --private-root /tmp/private 2>&1")[0], "inspect")
        self.assertTrue(read_only_command('for f in app.py AGENTS.md; do echo "== $f"; cat "$f"; echo; done; which agentctl'))
        self.assertFalse(read_only_command('for f in app.py AGENTS.md; do rm "$f"; done'))
        self.assertFalse(read_only_command('for f in app.py AGENTS.md; do cat "$f" > /tmp/copied; done'))

    def test_fixture_trust_cleanup_restores_only_its_own_added_section(self):
        from scripts.provider_bootstrap_smoke import restore_fixture_trust
        config = self.root / "config.toml"
        before = b'model="example"\n'
        config.write_bytes(before + ('\n[projects.' + json.dumps(str(self.source)) + ']\ntrust_level="trusted"\n').encode())
        self.assertTrue(restore_fixture_trust(config, before, self.source))
        self.assertEqual(config.read_bytes(), before)

    def test_fixture_trust_cleanup_preserves_unrelated_editor_changes(self):
        from scripts.provider_bootstrap_smoke import restore_fixture_trust
        config = self.root / "config.toml"
        before = b'model="example"\n'
        current = b'model="editor-change"\n' + ('\n[projects.' + json.dumps(str(self.source)) + ']\ntrust_level="trusted"\n').encode()
        config.write_bytes(current)
        with self.assertRaisesRegex(EvidenceError, "provider_settings_change_outside_fixture"):
            restore_fixture_trust(config, before, self.source)
        self.assertEqual(config.read_bytes(), current)

    def test_readonly_status_is_valid_bootstrap_discovery(self):
        events, calls = self.fixture()
        events[1]["item"]["command"] = events[1]["item"]["command"].replace("agentctl inspect", "agentctl status")
        calls[1]["command"] = "status"
        self.assertTrue(verify_trace(events, calls, self.source, self.managed, self.token)["inspect_before_resume"])

    def test_mainline_option_does_not_hide_the_inspection_command(self):
        from scripts.provider_bootstrap_smoke import cli_phase
        self.assertEqual(cli_phase(["--mainline-ref", "refs/heads/main", "inspect"]), "inspect")

    def test_wrapper_source_is_valid_python_and_has_tight_command_allowlist(self):
        wrapper = self.root / "agentctl"
        config = self.root / "config.json"
        make_wrapper(wrapper, config)
        compile(wrapper.read_text(encoding="utf-8"), str(wrapper), "exec")
        self.assertEqual(wrapper.stat().st_mode & 0o777, 0o700)


from test_publication import RepositoryFixture


class ProviderBootstrapFinalStateTests(RepositoryFixture):
    def test_source_revision_stays_distinct_from_owned_managed_revision(self):
        from agent_handoff.git_backend import GitBackend
        from scripts.provider_bootstrap_smoke import verify_final_state
        (self.checkout / "app.py").write_text("def answer():\n    return 41\n")
        self.git(self.checkout, "add", "--", "app.py")
        self.git(self.checkout, "commit", "-qm", "fixture app")
        source_revision = self.git(self.checkout, "rev-parse", "HEAD")
        self.runtime.acquire()
        checkpoint = self.runtime.publish_snapshot("checkpoint", ["app.py"], summary="Fixture app")
        revision = checkpoint["published_revision"]
        managed = self.root / "managed"
        self.git(self.root, "clone", "--quiet", "--no-checkout", str(self.remote), str(managed))
        self.git(managed, "checkout", "--quiet", "--detach", revision)
        (managed / "app.py").write_text("def answer() -> int:\n    return 42\n")
        observer = GitBackend(self.root / "observer.git", str(self.remote), self.authority.publication_ref)
        mainline = GitBackend(self.root / "mainline.git", str(self.remote), "refs/heads/main")
        self.assertNotEqual(source_revision, revision)
        result = verify_final_state(self.checkout, managed, observer, mainline,
                                    self.base, source_revision, revision)
        self.assertTrue(result["source_unchanged"])
        self.assertTrue(result["managed_app_returns_42"])
        self.assertTrue(result["no_feature_publication"])

    def test_generated_python_cache_is_verified_against_current_source(self):
        import py_compile
        from agent_handoff.git_backend import GitBackend
        from scripts.provider_bootstrap_smoke import verify_final_state
        self.test_source_revision_stays_distinct_from_owned_managed_revision()
        managed = self.root / "managed"
        py_compile.compile(str(managed / "app.py"), doraise=True)
        revision = self.observe()
        source_revision = self.git(self.checkout, "rev-parse", "HEAD")
        observer = GitBackend(self.root / "observer.git", str(self.remote), self.authority.publication_ref)
        mainline = GitBackend(self.root / "mainline.git", str(self.remote), "refs/heads/main")
        result = verify_final_state(self.checkout, managed, observer, mainline, self.base, source_revision, revision)
        self.assertTrue(result["verified_python_cache_present"])
        (managed / "unexpected.py").write_text("unexpected")
        with self.assertRaisesRegex(EvidenceError, "unexpected_managed_checkout_changes"):
            verify_final_state(self.checkout, managed, observer, mainline, self.base, source_revision, revision)


if __name__ == "__main__":
    unittest.main()
