import unittest
import hashlib

from agent_handoff.contracts import RuntimeFailure
from agent_handoff.preservation import preserve, status_counts, read_record
from test_publication import RepositoryFixture
from test_record_schemas import validator


class StatusEvidenceTests(unittest.TestCase):
    def test_rename_source_is_not_a_status_record(self):
        self.assertEqual(status_counts(b"R  new.txt\x00??old.txt\x00?? fresh.txt\x00!! ignored.txt\x00"), (1, 1, 1))
        with self.assertRaises(RuntimeFailure):
            status_counts(b"R  new.txt\x00")


class PreservationRecordTests(RepositoryFixture):
    def test_corrupt_preservation_bytes_counts_or_identity_fail_closed(self):
        original = preserve(self.checkout, self.runtime.store)
        name = "preservation-" + original["handle_id"] + ".json"
        invalid_index = b"100644 " + b"a" * 40 + b" 4\tcode.txt\x00"
        corruptions = [{"index_evidence": "0" * 64}, {"index_entries_hex": "abc"},
                       {"tracked_changes": original["tracked_changes"] + 1},
                       {"status_entries_hex": b"ZZ code.txt\x00".hex()},
                       {"status_entries_hex": b" M code.txt".hex()},
                       {"handle_id": "f" * 32}, {"original_checkout": "/different-checkout"},
                       {"local_commits": ["a" * 40, "a" * 40]},
                       {"index_entries_hex": invalid_index.hex(), "index_evidence": hashlib.sha256(invalid_index).hexdigest()}]
        for changes in corruptions:
            self.runtime.store.write(name, dict(original, **changes))
            with self.subTest(fields=list(changes)), self.assertRaises(RuntimeFailure):
                read_record(self.runtime.store, original["handle_id"], checkout=self.checkout)
        self.runtime.store.write(name, original)
        self.assertEqual(read_record(self.runtime.store, original["handle_id"], checkout=self.checkout), original)

    def test_real_rename_retains_private_evidence_without_false_untracked(self):
        path = self.checkout / "??old.txt"
        path.write_text("retained content")
        self.git(self.checkout, "--literal-pathspecs", "add", "--", "??old.txt")
        self.git(self.checkout, "commit", "-qm", "local file")
        self.git(self.checkout, "mv", "--", "??old.txt", "new.txt")
        before = (self.checkout / ".git/index").read_bytes()
        report = preserve(self.checkout, self.runtime.store)
        schema = validator("preservation")
        schema.validate(report)
        for changes in ({"schema_version": True}, {"tracked_changes": -1}, {"index_evidence": "invalid"}):
            self.assertFalse(schema.is_valid(dict(report, **changes)))
        self.assertEqual(report["tracked_changes"], 1)
        self.assertEqual(report["untracked"], 0)
        self.assertEqual((self.checkout / ".git/index").read_bytes(), before)
        self.assertEqual((self.checkout / "new.txt").read_text(), "retained content")
