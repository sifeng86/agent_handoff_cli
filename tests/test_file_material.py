import os
from pathlib import Path
import tempfile
import unittest

from agent_handoff.contracts import RuntimeFailure
from agent_handoff.paths import PrivateStore
from agent_handoff.secrets.file_material import FileMaterial, validate_pem


PEM = "-----BEGIN PRIVATE KEY-----\nYQ==\n-----END PRIVATE KEY-----\n"


class FileMaterialTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.checkout = root / "repo"
        self.checkout.mkdir()
        self.store = PrivateStore(root / "private", self.checkout)
        self.addCleanup(self.store.close)
        self.material = FileMaterial(self.checkout, "keys/key.pem", self.store)

    def test_editor_change_before_rename_preserves_edit_and_backup(self):
        self.material.apply(PEM, expected=None)
        edited = PEM.replace("YQ==", "Yg==")
        def editor(_):
            (self.checkout / "keys/key.pem").write_text(edited)
        with self.assertRaises(RuntimeFailure) as rejected:
            self.material.apply(PEM.replace("YQ==", "Yw=="), expected=PEM, fault=editor)
        self.assertEqual(rejected.exception.status, "rejected")
        self.assertEqual(self.material.read(), edited)
        self.assertFalse(list((self.checkout / "keys").glob(".agent-handoff-material-*")))
        backups = list(self.store.root.glob("secret-file-backup-*.json"))
        self.assertTrue(any(self.store.read(path.name)["raw"] == PEM for path in backups))
        self.assertTrue(all(path.stat().st_mode & 0o777 == 0o600 for path in backups))

    def test_symlink_directory_and_hardlink_are_never_written(self):
        outside = self.checkout.parent / "outside"
        outside.mkdir()
        (self.checkout / "keys").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(RuntimeFailure):
            self.material.apply(PEM, expected=None)
        self.assertFalse(list(outside.iterdir()))
        (self.checkout / "keys").unlink()
        self.material.apply(PEM, expected=None)
        os.link(self.checkout / "keys/key.pem", outside / "alias.pem")
        with self.assertRaises(RuntimeFailure):
            self.material.apply(PEM.replace("YQ==", "Yg=="), expected=PEM)
        self.assertEqual((outside / "alias.pem").read_text(), PEM)

    def test_invalid_pem_binary_and_escaping_mapping_fail_closed(self):
        for value in ("plain text", PEM.replace("YQ==", "!!!"), PEM + "unframed tail", PEM.replace("END PRIVATE", "END PUBLIC")):
            with self.subTest(value=value), self.assertRaises(RuntimeFailure):
                validate_pem(value)
        for mapping in ("../outside.pem", "keys/key.p12", ".git/config"):
            with self.subTest(mapping=mapping), self.assertRaises(RuntimeFailure):
                FileMaterial(self.checkout, mapping, self.store)
        self.assertFalse(list(self.checkout.iterdir()))
