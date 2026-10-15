"""Local bare-remote end-to-end coverage for separate-process resume."""
from pathlib import Path
import subprocess
import tempfile
import unittest

from agent_handoff.git_backend import git_environment
from scripts.hosting_resume_smoke import run


def git(cwd, *args):
    return subprocess.check_output(["git", "-C", str(cwd), *args], env=git_environment(),
                                   stderr=subprocess.PIPE, text=True, timeout=30).strip()


class HostingResumeSmokeTests(unittest.TestCase):
    def test_local_bare_remote_resumes_exact_snapshot_and_preserves_source(self):
        with tempfile.TemporaryDirectory(prefix="hosting-resume-smoke-test-") as temporary:
            root = Path(temporary)
            remote = root / "remote.git"
            remote.mkdir()
            subprocess.run(["git", "-C", str(remote), "init", "--bare", "--quiet"], check=True,
                           env=git_environment(), capture_output=True, timeout=30)
            source = root / "mainline"
            source.mkdir()
            subprocess.run(["git", "-C", str(source), "init", "--quiet", "-b", "main"], check=True,
                           env=git_environment(), capture_output=True, timeout=30)
            (source / "base.txt").write_text("mainline remains unchanged\n", encoding="utf-8")
            git(source, "add", "--", "base.txt")
            git(source, "commit", "--quiet", "-m", "base")
            base = git(source, "rev-parse", "HEAD")
            git(source, "push", "--quiet", str(remote), "refs/heads/main")

            report = run(str(remote), "refs/heads/main", base)

            self.assertTrue(report["passed"], report.get("error"))
            self.assertTrue(report["branch_deleted"])
            self.assertTrue(report["mainline_unchanged"])
            self.assertEqual(report["mainline_revision"], base)
            self.assertEqual(report["checkpoint_status"], "completed")
            self.assertEqual(report["resume_status"], "not_ready")
            self.assertEqual(report["managed_recovery_status"], "completed")
            self.assertEqual(report["managed_resume_status"], "ready")
            self.assertEqual(report["original_recovery_status"], "rejected")
            self.assertEqual(report["release_status"], "completed")
            self.assertIsNone(report["provider_version"])
            self.assertTrue(report["managed_checkout_private"])
            self.assertTrue(report["resume_snapshot_exact"])
            self.assertTrue(report["preserved_edits_require_integration"])
            self.assertTrue(report["source_state_preserved"])
            self.assertTrue(report["recover_session_proved"])
            self.assertTrue(report["original_checkout_fenced"])
            self.assertTrue(report["resume_idempotent"])
            refs = subprocess.run(["git", "-C", str(remote), "show-ref", "--verify", report["publication_ref"]],
                                  env=git_environment(), capture_output=True, timeout=30)
            self.assertNotEqual(refs.returncode, 0)
            self.assertEqual(git(remote, "rev-parse", "refs/heads/main"), base)


if __name__ == "__main__":
    unittest.main()
