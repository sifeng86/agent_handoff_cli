import copy
from pathlib import Path
import tempfile
import unittest

from agent_handoff.contracts import RuntimeFailure
from agent_handoff.paths import PrivateStore
from agent_handoff.secrets.journal import SecretJournal
from agent_handoff.secrets.adapter import destination_identity
from test_record_schemas import validator


class SecretJournalRecordTests(unittest.TestCase):
    def test_corrupt_records_cannot_clear_pending_barrier(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            checkout = root / "repo"
            checkout.mkdir()
            with PrivateStore(root / "private", checkout) as store:
                journal = SecretJournal(store)
                manifest = {"schema_version": 1, "keys": {"A": {"classification": "shared", "required": True}}}
                binding = {"schema_version": 1, "repository_identity": "a" * 64,
                           "destination_identity": destination_identity("doppler", "fixture", "dev"),
                           "mode": "doppler", "project": "fixture", "config": "dev",
                           "manifest": manifest, "upload_authorized": True}
                evidence = {"repository_identity": "a" * 64, "observed_head": "d" * 40,
                            "session_id": "a" * 32, "checkout_identity": "c" * 64, "context_identity": "b" * 64,
                            "binding": binding, "manifest": manifest, "local_raw": "", "local_values": {},
                            "baseline": None, "local": {}, "remote": {}, "target": {"A": "dummy"},
                            "plan_id": "b" * 32, "decision_id": None}
                original = journal.create(evidence, {"A": "dummy"}, {"A": "dummy"})
                schema = validator("secret-journal")
                schema.validate(original)
                name = "secret-operation-" + original["operation_id"] + ".json"
                corruptions = [dict(original, schema_version=2), dict(original, extra=True),
                               dict(original, phase="resolved", result="unknown"), dict(original, keys={})]
                sent = copy.deepcopy(original)
                sent.update(phase="resolved", result="partial")
                sent["keys"]["A"]["status"] = "unknown"
                self.assertFalse(schema.is_valid(sent))
                corruptions.append(sent)
                copied = dict(original, operation_id="f" * 32)
                corruptions.append(copied)
                for field, value in (("binding", {}), ("session_id", "public-label"), ("target", {"UNKNOWN": "dummy"}),
                                     ("local_raw", "A=private-sentinel"), ("baseline", {"A": None})):
                    corrupt = copy.deepcopy(original)
                    corrupt["evidence"][field] = value
                    corruptions.append(corrupt)
                corruptions.append(dict(original, uploads={"A": None}))
                for corrupt in corruptions:
                    store.write(name, corrupt)
                    with self.assertRaises(RuntimeFailure):
                        journal.require_clear()
                store.write(name, original)
                self.assertEqual(len(journal.pending()), 1)
