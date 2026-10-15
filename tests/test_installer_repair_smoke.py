from pathlib import Path
import tempfile
import unittest

from scripts.installer_repair_smoke import _broken_launcher_fails, _tree


class InstallerRepairSmokeEvidenceTests(unittest.TestCase):
    def test_tree_snapshot_detects_unrelated_file_mode_and_link_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "bin/agentctl"
            target.parent.mkdir()
            target.write_bytes(b"launcher")
            target.chmod(0o755)
            (root / "lib").mkdir()
            payload = root / "lib/module.py"
            payload.write_bytes(b"module")
            payload.chmod(0o644)
            link = root / "current"
            link.symlink_to("lib/module.py")

            baseline = _tree(root, {"bin/agentctl"})
            payload.write_bytes(b"changed")
            self.assertNotEqual(baseline, _tree(root, {"bin/agentctl"}))

            payload.write_bytes(b"module")
            payload.chmod(0o600)
            self.assertNotEqual(baseline, _tree(root, {"bin/agentctl"}))

            payload.chmod(0o644)
            link.unlink()
            link.symlink_to("lib/other.py")
            self.assertNotEqual(baseline, _tree(root, {"bin/agentctl"}))

    def test_target_can_be_excluded_for_expected_launcher_replacement(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            launcher = root / "bin/agentctl"
            launcher.parent.mkdir()
            launcher.write_bytes(b"old")
            before = _tree(root, {"bin/agentctl"})
            launcher.write_bytes(b"new")
            self.assertEqual(before, _tree(root, {"bin/agentctl"}))

    def test_missing_interpreter_fixture_requires_enoent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            launcher = root / "agentctl"
            launcher.write_bytes(b"#!/missing/agent-handoff-python\nexit 0\n")
            launcher.chmod(0o755)
            log = root / "private.log"
            log.touch(mode=0o600)
            self.assertTrue(_broken_launcher_fails(launcher, cwd=root, env=dict(), log=log))
            self.assertEqual(log.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
