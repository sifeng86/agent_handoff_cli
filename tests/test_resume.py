import contextlib
import io
import json
from pathlib import Path
from unittest.mock import patch

from agent_handoff.cli import main
from agent_handoff.contracts import RuntimeFailure
from test_publication import RepositoryFixture


class ResumeTests(RepositoryFixture):
    def test_takeover_during_existing_owner_materialization_never_claims_owned(self):
        self.runtime.acquire()
        index = (self.checkout / ".git/index").read_bytes()
        with self.new_runtime("other-private", "b" * 64) as other:
            other.initialize(self.base, mainline_ref="refs/heads/main")
            from agent_handoff.materialization import materialize
            def transfer(*args):
                managed = materialize(*args)
                proposal = other.acquire()
                other.apply_decision(proposal["decision"]["decision_id"], "takeover")
                return managed
            with patch("agent_handoff.publication.materialize", side_effect=transfer):
                resumed = self.runtime.resume()
            self.assertEqual(resumed["status"], "not_ready")
            self.assertEqual(resumed["ownership"], "fenced")
            self.assertEqual(other.recover()["ownership"], "owned")
        self.assertEqual((self.checkout / ".git/index").read_bytes(), index)

    def test_A28_rebase_in_progress_retained_while_resume_isolates_checkout(self):
        import subprocess
        self.git(self.checkout, "checkout", "-qb", "other")
        (self.checkout / "code.txt").write_text("other branch\n")
        self.git(self.checkout, "commit", "-qam", "other edit")
        self.git(self.checkout, "checkout", "-q", "main")
        (self.checkout / "code.txt").write_text("main branch\n")
        self.git(self.checkout, "commit", "-qam", "main edit")
        with self.assertRaises(subprocess.CalledProcessError):
            self.git(self.checkout, "rebase", "other")
        rebase = self.checkout / ".git/rebase-merge"
        self.assertTrue(rebase.is_dir())
        metadata = {path.relative_to(rebase).as_posix(): path.read_bytes() for path in rebase.rglob("*") if path.is_file()}
        head = self.git(self.checkout, "rev-parse", "HEAD")
        index = (self.checkout / ".git/index").read_bytes()
        content = (self.checkout / "code.txt").read_bytes()
        resumed = self.runtime.resume()
        self.assertEqual(resumed["status"], "not_ready")
        self.assertNotEqual(Path(resumed["managed_checkout"]), self.checkout)
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), head)
        self.assertEqual((self.checkout / ".git/index").read_bytes(), index)
        self.assertEqual((self.checkout / "code.txt").read_bytes(), content)
        self.assertEqual({path.relative_to(rebase).as_posix(): path.read_bytes() for path in rebase.rglob("*") if path.is_file()}, metadata)
        evidence = self.runtime.store.read("preservation-" + resumed["preservation_handle"] + ".json")
        self.assertTrue(evidence["merge_or_rebase"])
        self.git(self.checkout, "rebase", "--abort")
        self.assertFalse(rebase.exists())
        self.assertEqual((self.checkout / "code.txt").read_text(), "main branch\n")
        self.assertEqual(self.git(self.checkout, "symbolic-ref", "--short", "HEAD").strip(), "main")

    def test_A28_merge_in_progress_retained_while_resume_isolates_checkout(self):
        import subprocess
        self.git(self.checkout, "checkout", "-qb", "other")
        (self.checkout / "code.txt").write_text("other branch\n")
        self.git(self.checkout, "commit", "-qam", "other edit")
        self.git(self.checkout, "checkout", "-q", "main")
        (self.checkout / "code.txt").write_text("main branch\n")
        self.git(self.checkout, "commit", "-qam", "main edit")
        with self.assertRaises(subprocess.CalledProcessError):
            self.git(self.checkout, "merge", "other")
        merge_head = (self.checkout / ".git/MERGE_HEAD").read_bytes()
        index = (self.checkout / ".git/index").read_bytes()
        content = (self.checkout / "code.txt").read_bytes()
        resumed = self.runtime.resume()
        self.assertEqual(resumed["status"], "not_ready")
        self.assertNotEqual(Path(resumed["managed_checkout"]), self.checkout)
        self.assertEqual((self.checkout / ".git/MERGE_HEAD").read_bytes(), merge_head)
        self.assertEqual((self.checkout / ".git/index").read_bytes(), index)
        self.assertEqual((self.checkout / "code.txt").read_bytes(), content)
        evidence = self.runtime.store.read("preservation-" + resumed["preservation_handle"] + ".json")
        self.assertTrue(evidence["merge_or_rebase"])

    def test_clean_resume_materializes_and_transfers_binding(self):
        original = (self.checkout / ".git/index").read_bytes()
        resumed = self.runtime.resume()
        self.assertEqual(resumed["status"], "ready")
        managed = Path(resumed["managed_checkout"])
        self.assertNotEqual(managed, self.checkout)
        self.assertEqual(self.git(managed, "rev-parse", "HEAD"), resumed["observed_revision"])
        self.assertEqual((self.checkout / ".git/index").read_bytes(), original)
        with self.assertRaises(RuntimeFailure):
            self.runtime.recover()
        with self.new_runtime("private", self.context, managed) as owner:
            self.assertEqual(owner.recover()["ownership"], "owned")
            self.assertEqual(owner.resume()["status"], "ready")

    def test_A25_A26_dirty_index_binary_ignored_preserved(self):
        (self.checkout / "code.txt").write_text("staged")
        self.git(self.checkout, "add", "code.txt")
        (self.checkout / "code.txt").write_text("unstaged")
        (self.checkout / "untracked.bin").write_bytes(b"\x00\xff")
        (self.checkout / ".git/info/exclude").write_text("ignored.bin\n")
        (self.checkout / "ignored.bin").write_bytes(b"private\x00")
        index = (self.checkout / ".git/index").read_bytes()
        resumed = self.runtime.resume()
        self.assertEqual(resumed["status"], "not_ready")
        self.assertEqual((self.checkout / ".git/index").read_bytes(), index)
        self.assertEqual((self.checkout / "code.txt").read_text(), "unstaged")
        self.assertEqual((self.checkout / "ignored.bin").read_bytes(), b"private\x00")
        self.assertFalse((Path(resumed["managed_checkout"]) / "untracked.bin").exists())

    def test_A27_local_only_commit_is_retained(self):
        (self.checkout / "code.txt").write_text("local commit")
        self.git(self.checkout, "add", "code.txt")
        self.git(self.checkout, "commit", "-qm", "local-only")
        head = self.git(self.checkout, "rev-parse", "HEAD")
        resumed = self.runtime.resume()
        self.assertEqual(resumed["status"], "not_ready")
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), head)
        self.assertEqual((Path(resumed["managed_checkout"]) / "code.txt").read_text(), "original\n")

    def test_materialization_failure_releases_new_acquisition(self):
        with patch("agent_handoff.publication.materialize", side_effect=RuntimeFailure("Injected failure.")):
            resumed = self.runtime.resume()
        self.assertEqual(resumed["status"], "not_ready")
        self.assertEqual(resumed["ownership"], "released")

    def test_integrity_adopt_binds_exact_valid_revision(self):
        first = self.observe()
        self.runtime.acquire()
        self.git(self.remote, "update-ref", self.authority.publication_ref, first)
        proposal = self.runtime.propose_integrity()
        self.assertIn("adopt", [option["option_id"] for option in proposal["decision"]["options"]])
        adopted = self.runtime.apply_decision(proposal["decision"]["decision_id"], "adopt")
        self.assertEqual(adopted["status"], "completed")
        self.assertEqual(self.runtime.acquire()["ownership"], "owned")

    def test_invalid_integrity_snapshot_only_offers_abort(self):
        self.git(self.remote, "update-ref", self.authority.publication_ref, self.base)
        proposal = self.runtime.propose_integrity()
        self.assertEqual([option["option_id"] for option in proposal["decision"]["options"]], ["abort"])
        self.assertEqual(self.runtime.apply_decision(proposal["decision"]["decision_id"], "abort")["status"], "completed")

    def test_cli_context_init_acquire_and_release_json(self):
        common = ["--authority", str(self.remote), "--publication-ref", self.authority.publication_ref,
                  "--checkout", str(self.checkout), "--private-root", str(self.root / "cli-private")]
        def invoke(argv):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(argv)
            return code, json.loads(output.getvalue())
        code, created = invoke(["context", "create", "--checkout", str(self.checkout),
                                "--private-root", str(self.root / "cli-private")])
        self.assertEqual(code, 0)
        self.assertNotIn("context", created)
        self.assertEqual(invoke(["init", *common])[0], 0)
        context = ["--context-file", created["context_file"]]
        self.assertEqual(invoke(["acquire", *common, *context])[1]["ownership"], "owned")
        self.assertEqual(invoke(["release", *common, *context])[1]["ownership"], "released")
