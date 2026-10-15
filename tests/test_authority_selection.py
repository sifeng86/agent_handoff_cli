"""Authority selection crash recovery and caller isolation."""
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest

from agent_handoff.authority_selection import AuthoritySelection
from agent_handoff.contracts import RuntimeFailure
from agent_handoff.paths import PrivateStore


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.checkout = root / "checkout"
        self.checkout.mkdir()
        self.store = PrivateStore(root / "private", self.checkout)
        self.selection = AuthoritySelection(self.store, "a" * 64, "b" * 64)
        self.source, self.target = "c" * 64, "d" * 64
        self.selection.register(self.source)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_crash_boundaries_resolve_without_replaying_selection(self):
        for boundary, expected in (("after_transition_journal", "not_selected"),
                                   ("after_selection_commit", "selected")):
            with self.subTest(boundary=boundary):
                def crash(point):
                    if point == boundary:
                        raise SystemExit(77)
                with self.selection.locked():
                    with self.assertRaises(SystemExit):
                        self.selection.switch(self.source, self.target, "e" * 32, fault=crash)
                    with self.assertRaises(RuntimeFailure) as caught:
                        self.selection.require(self.source)
                    self.assertEqual(caught.exception.status, "unknown")
                    actual = self.selection.read()
                    resolved = self.selection.resolve()
                    self.assertEqual(resolved["outcome"], expected)
                    self.assertEqual(self.selection.read(), actual)
                    self.assertEqual(self.selection.resolve(), resolved)
                if expected == "selected":
                    self.selection.require(self.target)
                    with self.assertRaises(RuntimeFailure):
                        self.selection.register(self.source)

    def test_corrupt_or_unrelated_commit_point_remains_unknown(self):
        with self.assertRaises(SystemExit):
            self.selection.switch(self.source, self.target, "e" * 32,
                                  fault=lambda _: (_ for _ in ()).throw(SystemExit(77)))
        name = self.selection.prefix + ".json"
        original = self.selection.read()
        self.store.write(name, dict(original, repository_identity="f" * 64))
        with self.assertRaises(RuntimeFailure) as caught:
            self.selection.resolve()
        self.assertEqual(caught.exception.status, "unknown")
        self.store.write(name, dict(original, context_identity="f" * 64))
        with self.assertRaises(RuntimeFailure):
            self.selection.resolve()
        self.store.write(name, original)
        journal_name = self.selection.prefix + "-transition.json"
        journal = self.store.read(journal_name)
        self.store.write(journal_name, dict(journal, after=original))
        with self.assertRaises(RuntimeFailure):
            self.selection.resolve()
        self.store.write(journal_name, journal)
        self.assertEqual(self.selection.resolve()["outcome"], "not_selected")

    def test_distinct_callers_and_no_capability_or_secret_migration(self):
        self.store.write("secret-binding.json", {"schema_version": 1, "sentinel": "private"})
        before = self.store.read("secret-binding.json")
        with self.selection.locked():
            result = self.selection.switch(self.source, self.target, "e" * 32)
        self.assertEqual(result["outcome"], "selected")
        self.assertEqual(self.store.read("secret-binding.json"), before)
        self.assertFalse(any(name.startswith("session-") for name in self.store.names()))
        other = AuthoritySelection(self.store, "a" * 64, "f" * 64)
        self.assertIsNone(other.read())
        other.register(self.source)
        self.selection.require(self.target)
        other.require(self.source)
        self.store.write(self.selection.prefix + ".json",
                         dict(self.selection.read(), repository_identity=self.source))
        with self.assertRaises(RuntimeFailure) as caught:
            self.selection.resolve()
        self.assertEqual(caught.exception.status, "unknown")

    def test_process_exit_after_commit_recovers_from_disk(self):
        code = """
import os, sys
from pathlib import Path
from agent_handoff.paths import PrivateStore
from agent_handoff.authority_selection import AuthoritySelection
with PrivateStore(Path(sys.argv[1]), Path(sys.argv[2])) as store:
    selection = AuthoritySelection(store, 'a' * 64, 'b' * 64)
    with selection.locked():
        selection.switch('c' * 64, 'd' * 64, 'e' * 32,
                         fault=lambda point: os._exit(77) if point == 'after_selection_commit' else None)
"""
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
        child = subprocess.run([sys.executable, "-c", code, str(self.store.root), str(self.checkout)],
                               env=env, capture_output=True, timeout=15)
        self.assertEqual(child.returncode, 77)
        with self.selection.locked():
            self.assertEqual(self.selection.resolve()["outcome"], "selected")
            self.selection.require(self.target)
