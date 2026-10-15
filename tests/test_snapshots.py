import json
from pathlib import Path
import unittest
from unittest.mock import patch

from agent_handoff.contracts import RuntimeFailure
from agent_handoff.preservation import snapshot, _read
from test_publication import RepositoryFixture


class SnapshotTests(RepositoryFixture):
    def test_A38_step_insertion_and_reorder_preserve_cursor_identity(self):
        from agent_handoff.plans import parse
        self.runtime.acquire()
        state = self.create_plan()
        self.runtime.publish_snapshot("checkpoint", ["plans/plan.md"], summary="Initial steps", state=state)
        path = self.checkout / "plans/plan.md"
        metadata = parse(path.read_text())
        metadata["steps"] = ["S-NEW", "S-002", "S-001"]
        path.write_text("<!-- agent-handoff-plan\n" + json.dumps(metadata) + "\n-->\n# Plan\n")
        report = self.runtime.publish_snapshot("checkpoint", ["plans/plan.md"], summary="Reordered stable IDs")
        revision = report["published_revision"]
        self.assertEqual(self.runtime.backend.records(revision)["state"]["current_step"], "S-001")
        published = parse(self.git(self.remote, "show", revision + ":plans/plan.md") + "\n")
        self.assertEqual(published["id"], "P-001")
        self.assertEqual(published["steps"], ["S-NEW", "S-002", "S-001"])

    def test_A12_checkpoint_keeps_owner_and_recovers_exact_selected_snapshot(self):
        self.runtime.acquire()
        session = self.runtime.sessions.current()
        content = b"selected binary\x00snapshot\n"
        (self.checkout / "code.txt").write_bytes(content)
        report = self.runtime.publish_snapshot("checkpoint", ["code.txt"], summary="Recoverable checkpoint")
        self.assertEqual(report["ownership"], "owned")
        revision = report["published_revision"]
        self.assertEqual(self.runtime.backend.run("show", revision + ":code.txt").stdout, content)
        with self.new_runtime("private", self.context) as restarted:
            self.assertEqual(restarted.recover()["ownership"], "owned")
            self.assertEqual(restarted.sessions.current()["session_id"], session["session_id"])
            self.assertEqual(restarted.backend.observe(), revision)

    def test_A40_claimed_test_summary_without_revision_evidence_is_unknown(self):
        self.runtime.acquire()
        (self.checkout / "code.txt").write_text("selected code\n")
        published = self.runtime.publish_snapshot("checkpoint", ["code.txt"], summary="Tests passed in another checkout")
        self.assertEqual(published["code_publication"], "published")
        self.assertEqual(published["test_status"], "unknown")
        self.assertEqual(self.git(self.remote, "show", published["published_revision"] + ":code.txt"), "selected code")

    def test_A36_missing_active_plan_rejected_before_publication(self):
        self.runtime.acquire()
        state = self.create_plan()
        state["active_plan"] = "P-MISSING"
        before = self.observe()
        with self.assertRaises(RuntimeFailure):
            self.runtime.publish_snapshot("checkpoint", ["plans/plan.md"], summary="Missing Plan", state=state)
        self.assertEqual(self.observe(), before)

    def test_A55_tracked_dotenv_selection_rejected_without_index_changes(self):
        self.runtime.acquire()
        path = self.checkout / ".env"
        path.write_text("A=dummy-private-material\n")
        self.git(self.checkout, "add", "--", ".env")
        before = self.observe()
        index = (self.checkout / ".git/index").read_bytes()
        with self.assertRaises(RuntimeFailure):
            self.runtime.publish_snapshot("checkpoint", ["code.txt", ".env"], summary="Selected files")
        self.assertEqual(self.observe(), before)
        self.assertEqual((self.checkout / ".git/index").read_bytes(), index)
        self.assertEqual(path.read_text(), "A=dummy-private-material\n")

    def create_plan(self):
        directory = self.checkout / "plans"
        directory.mkdir(exist_ok=True)
        metadata = {"schema_version": 1, "id": "P-001", "status": "READY", "goal": "Ship code", "scope": "Code",
                    "non_goals": "Secrets", "steps": ["S-001", "S-002"], "acceptance_criteria": "Tests pass"}
        (directory / "plan.md").write_text("<!-- agent-handoff-plan\n" + json.dumps(metadata) + "\n-->\n# Plan\n")
        return {"schema_version": 1, "status": "working", "active_plan": "P-001", "current_step": "S-001",
                "blocker": None, "summary": ""}

    def test_A15_post_snapshot_editor_changes_retained_for_checkpoint_and_handoff(self):
        self.runtime.acquire()
        source_head = self.git(self.checkout, "rev-parse", "HEAD")
        for command in ("checkpoint", "handoff"):
            with self.subTest(command=command):
                frozen = "frozen-" + command
                edited = "editor-after-snapshot-" + command
                (self.checkout / "code.txt").write_text(frozen)
                self.git(self.checkout, "add", "code.txt")
                original_index = (self.checkout / ".git/index").read_bytes()
                def editor(point):
                    if point == "before_remote_send":
                        (self.checkout / "code.txt").write_text(edited)
                        (self.checkout / "new-local.bin").write_bytes(b"\x00\xffretain")
                self.runtime.fault = editor
                published = self.runtime.publish_snapshot(command, ["code.txt"], summary="Fixed snapshot")
                self.assertEqual(published["code_publication"], "published")
                self.assertEqual(published["ownership"], "owned" if command == "checkpoint" else "released")
                self.assertEqual(self.git(self.remote, "show", published["published_revision"] + ":code.txt"), frozen)
                self.assertNotIn("new-local.bin", self.git(self.remote, "ls-tree", "-r", "--name-only", published["published_revision"]).splitlines())
                self.assertEqual((self.checkout / "code.txt").read_text(), edited)
                self.assertEqual((self.checkout / "new-local.bin").read_bytes(), b"\x00\xffretain")
                self.assertEqual((self.checkout / ".git/index").read_bytes(), original_index)
                self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), source_head)

    def test_A11_atomic_handoff_fixed_snapshot_and_owner_release(self):
        self.runtime.acquire()
        (self.checkout / "code.txt").write_bytes(b"changed\x00binary")
        (self.checkout / ".env").write_text("SECRET=retain")
        (self.checkout / "executable").write_text("#!/bin/sh\nexit 0\n")
        (self.checkout / "executable").chmod(0o755)
        (self.checkout / "link").symlink_to("code.txt")
        original_index = (self.checkout / ".git/index").read_bytes()
        previous = self.observe()
        previous_state = self.runtime.backend.records(previous)["state"]
        published = self.runtime.publish_snapshot("handoff", ["code.txt", "executable", "link"], summary="Saved changes")
        self.assertEqual(published["ownership"], "released")
        head = self.observe()
        self.assertEqual(self.git(self.remote, "rev-list", "--parents", "-n", "1", head).split(), [head, previous])
        records = self.runtime.backend.records(head)
        self.assertIsNone(records["owner"]["owner"])
        self.assertEqual(records["state"], dict(previous_state, summary="Saved changes"))
        self.assertEqual(self.runtime.backend.run("show", head + ":code.txt").stdout, b"changed\x00binary")
        self.assertIn("120000", self.git(self.remote, "ls-tree", head, "--", "link"))
        self.assertIn("100755", self.git(self.remote, "ls-tree", head, "--", "executable"))
        self.assertEqual((self.checkout / ".git/index").read_bytes(), original_index)
        self.assertEqual((self.checkout / ".env").read_text(), "SECRET=retain")

    def test_A13_A14_plan_and_done(self):
        self.runtime.acquire()
        state = self.create_plan()
        checkpoint = self.runtime.publish_snapshot("checkpoint", ["plans/plan.md"], summary="Plan ready", state=state)
        self.assertEqual(checkpoint["ownership"], "owned")
        self.assertEqual(checkpoint["test_status"], "unknown")
        before = self.observe()
        with self.assertRaises(RuntimeFailure):
            self.runtime.publish_snapshot("done", [], summary="Finished")
        self.assertEqual(self.observe(), before)
        completed = self.runtime.publish_snapshot("done", [], summary="Finished", release_intent=False)
        self.assertEqual(completed["ownership"], "owned")
        head = self.observe()
        self.assertEqual(json.loads(self.git(self.remote, "show", head + ":.agent/state.json"))["status"], "idle")
        self.assertIn('"status": "DONE"', self.git(self.remote, "show", head + ":plans/plan.md"))

    def test_A14_done_explicit_true_releases_in_the_completion_commit(self):
        from agent_handoff.plans import parse
        self.runtime.acquire()
        state = self.create_plan()
        self.runtime.publish_snapshot("checkpoint", ["plans/plan.md"], summary="Plan ready", state=state)
        before = self.observe()
        (self.checkout / "code.txt").write_text("final selected payload\n")
        source_head = self.git(self.checkout, "rev-parse", "HEAD")
        source_index = (self.checkout / ".git/index").read_bytes()
        source_plan = (self.checkout / "plans/plan.md").read_bytes()

        completed = self.runtime.publish_snapshot("done", ["code.txt"], summary="Finished and released", release_intent=True)
        head = self.observe()
        self.assertEqual(completed["ownership"], "released")
        self.assertEqual(completed["published_revision"], head)
        self.assertEqual(self.git(self.remote, "rev-list", "--parents", "-n", "1", head).split(), [head, before])
        records = self.runtime.backend.records(head)
        self.assertIsNone(records["owner"]["owner"])
        self.assertEqual(records["state"], dict(state, status="idle", active_plan=None, current_step=None,
                                               blocker=None, summary="Finished and released"))
        self.assertEqual(parse(self.git(self.remote, "show", head + ":plans/plan.md") + "\n")["status"], "DONE")
        self.assertEqual(self.git(self.remote, "show", head + ":code.txt"), "final selected payload")
        self.assertEqual(set(self.git(self.remote, "diff-tree", "--no-commit-id", "--name-only", "-r", head).splitlines()),
                         {".agent/owner.json", ".agent/state.json", "plans/plan.md", "code.txt"})
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), source_head)
        self.assertEqual((self.checkout / ".git/index").read_bytes(), source_index)
        self.assertEqual((self.checkout / "plans/plan.md").read_bytes(), source_plan)
        self.assertEqual((self.checkout / "code.txt").read_text(), "final selected payload\n")
        with self.assertRaises(RuntimeFailure) as fenced:
            self.runtime.recover()
        self.assertEqual(fenced.exception.code, 4)
        self.assertEqual(self.observe(), head)

    def test_A39_release_preserves_working_plan_and_cursor(self):
        self.runtime.acquire()
        state = self.create_plan()
        self.runtime.publish_snapshot("checkpoint", ["plans/plan.md"], summary="Working", state=state)
        before = self.observe()
        original_state = self.git(self.remote, "show", before + ":.agent/state.json")
        original_plan = self.git(self.remote, "show", before + ":plans/plan.md")
        self.runtime.release()
        self.assertEqual(self.git(self.remote, "show", self.observe() + ":.agent/state.json"), original_state)
        self.assertEqual(self.git(self.remote, "show", self.observe() + ":plans/plan.md"), original_plan)

    def test_A37_invalid_active_cursor_rejected_before_checkpoint_or_handoff(self):
        self.runtime.acquire()
        state = self.create_plan()
        self.runtime.publish_snapshot("checkpoint", ["plans/plan.md"], summary="Valid Plan", state=state)
        plan_path = self.checkout / "plans/plan.md"
        valid_plan = plan_path.read_bytes()
        cases = (("missing_cursor", "missing", False), ("removed_active_step", "S-001", True))
        for label, cursor, remove_active_step in cases:
            for command in ("checkpoint", "handoff"):
                with self.subTest(case=label, command=command):
                    plan_path.write_bytes(valid_plan)
                    selected = ["code.txt"]
                    if remove_active_step:
                        from agent_handoff.plans import parse
                        metadata = parse(valid_plan.decode("utf-8"))
                        metadata["steps"] = [step for step in metadata["steps"] if step != cursor]
                        plan_path.write_text("<!-- agent-handoff-plan\n" + json.dumps(metadata) + "\n-->\n# Plan\n")
                        selected.append("plans/plan.md")
                    state = {"schema_version": 1, "status": "working", "active_plan": "P-001",
                             "current_step": cursor, "blocker": None, "summary": ""}
                    local_code = f"retained-{label}-{command}\n".encode()
                    (self.checkout / "code.txt").write_bytes(local_code)
                    self.git(self.checkout, "add", "--", "code.txt", "plans/plan.md")
                    before_head = self.observe()
                    before_records = self.runtime.backend.records(before_head)
                    before_session = self.runtime.sessions.current()
                    before_source_head = self.git(self.checkout, "rev-parse", "HEAD")
                    before_index = (self.checkout / ".git/index").read_bytes()
                    before_plan = plan_path.read_bytes()
                    before_status = self.git(self.checkout, "status", "--porcelain=v1", "-z")
                    pending_before = self.runtime.journal.pending()

                    with patch.object(self.runtime.backend, "push", wraps=self.runtime.backend.push) as push:
                        with self.assertRaises(RuntimeFailure):
                            self.runtime.publish_snapshot(command, selected, summary="Invalid current step", state=state)
                        push.assert_not_called()

                    self.assertEqual(self.observe(), before_head)
                    self.assertEqual(self.runtime.backend.records(before_head), before_records)
                    self.assertEqual(before_records["owner"]["owner"]["session_id"], before_session["session_id"])
                    self.assertEqual(self.runtime.sessions.current()["session_id"], before_session["session_id"])
                    self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), before_source_head)
                    self.assertEqual((self.checkout / ".git/index").read_bytes(), before_index)
                    self.assertEqual((self.checkout / "code.txt").read_bytes(), local_code)
                    self.assertEqual(plan_path.read_bytes(), before_plan)
                    self.assertEqual(self.git(self.checkout, "status", "--porcelain=v1", "-z"), before_status)
                    self.assertEqual(self.runtime.journal.pending(), pending_before)
                    self.assertEqual(pending_before, [])

        state["current_step"] = "S-001"
        plan_path.write_bytes(valid_plan)
        self.runtime.publish_snapshot("checkpoint", ["plans/plan.md"], summary="Valid", state=state)
        (self.checkout / "plans/plan.md").unlink()
        before = self.observe()
        with self.assertRaises(RuntimeFailure):
            self.runtime.publish_snapshot("checkpoint", ["plans/plan.md"], summary="Delete")
        self.assertEqual(self.observe(), before)

    def test_A30_editor_changes_collection_is_rejected(self):
        count = 0
        def edited(checkout, path):
            nonlocal count
            count += 1
            if count == 2:
                (checkout / path).write_text("new editor change")
            return _read(checkout, path)
        with patch("agent_handoff.preservation._read", side_effect=edited), self.assertRaises(RuntimeFailure):
            snapshot(self.checkout, ["code.txt"])

    def test_secret_paths_and_symlink_aliases_are_rejected(self):
        (self.checkout / ".env").write_text("private")
        (self.checkout / "alias").symlink_to(".env")
        for name in (".env", "alias", "../escape", ".agent/owner.json"):
            with self.subTest(name=name), self.assertRaises(RuntimeFailure):
                snapshot(self.checkout, [name])


if __name__ == "__main__":
    unittest.main()
