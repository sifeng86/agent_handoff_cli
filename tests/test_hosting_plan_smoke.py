"""Local bare-remote end-to-end coverage for hosting Plan acceptance."""
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from agent_handoff.git_backend import git_environment
from scripts import hosting_plan_smoke
from scripts.hosting_plan_smoke import run


def git(cwd, *args):
    return subprocess.check_output(["git", "-C", str(cwd), *args], env=git_environment(),
                                   stderr=subprocess.PIPE, text=True, timeout=30).strip()


class HostingPlanSmokeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="hosting-plan-smoke-test-")
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        remote = root / "remote.git"
        remote.mkdir()
        subprocess.run(["git", "-C", str(remote), "init", "--bare", "--quiet"], check=True,
                       env=git_environment(), capture_output=True, timeout=30)
        source = root / "source"
        source.mkdir()
        subprocess.run(["git", "-C", str(source), "init", "--quiet", "-b", "main"], check=True,
                       env=git_environment(), capture_output=True, timeout=30)
        (source / "base.txt").write_text("mainline remains unchanged\n", encoding="utf-8")
        git(source, "add", "--", "base.txt")
        git(source, "commit", "--quiet", "-m", "base")
        base = git(source, "rev-parse", "HEAD")
        git(source, "push", "--quiet", str(remote), "refs/heads/main")
        self.remote, self.base = remote, base

    def test_local_bare_remote_covers_plan_takeover_handoff_and_done(self):
        remote, base = self.remote, self.base

        report = run(str(remote), "refs/heads/main", base)

        self.assertTrue(report["passed"], report.get("error"))
        self.assertTrue(report["branch_deleted"])
        self.assertTrue(report["mainline_unchanged"])
        self.assertEqual(report["mainline_revision"], base)
        self.assertEqual(report["fenced_operations"], ["checkpoint", "release"])
        self.assertTrue(report["plan_release_preserved"])
        self.assertTrue(report["takeover_fenced_old_writer"])
        self.assertTrue(report["takeover_new_session"])
        self.assertTrue(report["handoff_preserved_working_cursor"])
        self.assertTrue(report["done_completed_plan"])
        self.assertTrue(report["done_retained_owner"])
        refs = subprocess.run(["git", "-C", str(remote), "show-ref", "--verify", report["publication_ref"]],
                              env=git_environment(), capture_output=True, timeout=30)
        self.assertNotEqual(refs.returncode, 0)
        self.assertEqual(git(remote, "rev-parse", "refs/heads/main"), base)

    def test_cleanup_lease_preserves_concurrently_changed_branch(self):
        original_git = hosting_plan_smoke.git
        competed = []

        def competing_writer(*args, **kwargs):
            if args[0] == "push" and any(arg.startswith("--force-with-lease=") for arg in args):
                lease = next(arg for arg in args if arg.startswith("--force-with-lease="))
                ref, expected = lease.split("=", 1)[1].rsplit(":", 1)
                git(self.remote, "update-ref", ref, self.base, expected)
                competed.append(ref)
            return original_git(*args, **kwargs)

        with patch.object(hosting_plan_smoke, "git", side_effect=competing_writer):
            report = run(str(self.remote), "refs/heads/main", self.base)
        self.assertEqual(competed, [report["publication_ref"]])
        self.assertFalse(report["passed"])
        self.assertFalse(report["branch_deleted"])
        self.assertEqual(git(self.remote, "rev-parse", report["publication_ref"]), self.base)
        self.assertEqual(git(self.remote, "rev-parse", "refs/heads/main"), self.base)


if __name__ == "__main__":
    unittest.main()
