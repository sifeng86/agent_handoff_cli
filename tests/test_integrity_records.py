from unittest.mock import patch

from agent_handoff.contracts import RuntimeFailure
from test_publication import RepositoryFixture
from test_record_schemas import validator


class IntegrityRecordTests(RepositoryFixture):
    def test_integrity_decision_stales_when_publication_head_changes(self):
        initial = self.observe()
        self.runtime.acquire()
        self.git(self.remote, "update-ref", self.authority.publication_ref, initial)
        (self.checkout / "code.txt").write_bytes(b"staged local edit\n")
        self.git(self.checkout, "add", "code.txt")
        (self.checkout / "code.txt").write_bytes(b"unstaged local edit\n")
        (self.checkout / "local.bin").write_bytes(b"\x00\xfflocal")
        source_head = self.git(self.checkout, "rev-parse", "HEAD")
        source_index = (self.checkout / ".git/index").read_bytes()
        source_status = self.git(self.checkout, "status", "--porcelain=v1", "-z", "--untracked-files=all")
        source_files = {name: (self.checkout / name).read_bytes() for name in ("code.txt", "local.bin")}

        proposal = self.runtime.propose_integrity()
        decision_id = proposal["decision"]["decision_id"]
        self.assertIn("adopt", [option["option_id"] for option in proposal["decision"]["options"]])
        trust = self.runtime.store.read("integrity.json")

        competing, _ = self.runtime.backend.prepare(initial, {"external.txt": b"external publication\n"},
                                                    "External valid publication\n")
        self.assertEqual(self.runtime.backend.push(competing, initial), "published")
        owner_before_apply = self.runtime.backend.records(competing)["owner"]
        with patch.object(self.runtime.backend, "push") as push, self.assertRaises(RuntimeFailure) as stale:
            self.runtime.apply_decision(decision_id, "adopt")
        self.assertEqual(stale.exception.code, 3)
        self.assertEqual(stale.exception.status, "decision_required")
        push.assert_not_called()
        self.assertEqual(self.runtime.store.read("decision-state-" + decision_id + ".json")["status"], "stale")
        self.assertEqual(self.runtime.backend.remote_head(), competing)
        self.assertEqual(self.runtime.backend.records(competing)["owner"], owner_before_apply)
        self.assertEqual(self.runtime.store.read("integrity.json"), trust)
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), source_head)
        self.assertEqual((self.checkout / ".git/index").read_bytes(), source_index)
        self.assertEqual(self.git(self.checkout, "status", "--porcelain=v1", "-z", "--untracked-files=all"), source_status)
        self.assertEqual({name: (self.checkout / name).read_bytes() for name in source_files}, source_files)

    def test_corrupt_linked_preservation_stops_adoption_before_observation(self):
        proposal = self.runtime.propose_integrity()
        decision_id = proposal["decision"]["decision_id"]
        name = "preservation-" + proposal["preservation_handle"] + ".json"
        original = self.runtime.store.read(name)
        before = self.runtime.store.read("integrity.json")
        self.runtime.store.write(name, dict(original, index_evidence="0" * 64))
        with patch.object(self.runtime.backend, "observe") as observe:
            with self.assertRaises(RuntimeFailure):
                self.runtime.apply_decision(decision_id, "adopt")
            observe.assert_not_called()
        self.assertEqual(self.runtime.store.read("integrity.json"), before)
        self.runtime.store.write(name, original)
        self.assertEqual(self.runtime.apply_decision(decision_id, "adopt")["status"], "completed")

    def test_integrity_consent_stales_when_trust_changes_without_remote_head_change(self):
        earlier = self.observe()
        self.runtime.acquire()
        proposal = self.runtime.propose_integrity()
        decision_id = proposal["decision"]["decision_id"]
        validator("integrity-decision").validate(self.runtime.store.read("integrity-decision-" + decision_id + ".json"))
        changed = dict(self.runtime.store.read("integrity.json"), last_trusted_oid=earlier)
        self.runtime.store.write("integrity.json", changed)
        with patch.object(self.runtime.backend, "observe") as observe:
            with self.assertRaises(RuntimeFailure) as caught:
                self.runtime.apply_decision(decision_id, "adopt")
            self.assertEqual(caught.exception.status, "decision_required")
            observe.assert_not_called()
        self.assertEqual(self.runtime.store.read("integrity.json"), changed)
        self.assertEqual(self.runtime.store.read("decision-state-" + decision_id + ".json")["status"], "stale")

    def test_integrity_consent_stales_after_approved_secret_binding_change(self):
        from agent_handoff.secrets.service import SecretService
        proposal = self.runtime.propose_integrity()
        decision_id = proposal["decision"]["decision_id"]
        before = self.runtime.store.read("integrity.json")
        service = SecretService(self.runtime, {"schema_version": 1, "keys": {"A": {"classification": "shared", "required": False}}})
        binding = service.propose_binding("local-env")
        service.apply_decision(binding["decision"]["decision_id"], "approve-secret-binding")
        with patch.object(self.runtime.backend, "observe") as observe:
            with self.assertRaises(RuntimeFailure):
                self.runtime.apply_decision(decision_id, "adopt")
            observe.assert_not_called()
        self.assertEqual(self.runtime.store.read("integrity.json"), before)

    def test_corrupt_integrity_observations_rejected_before_external_read(self):
        proposal = self.runtime.propose_integrity()
        decision_id = proposal["decision"]["decision_id"]
        name = "integrity-decision-" + decision_id + ".json"
        original = self.runtime.store.read(name)
        before = self.runtime.store.read("integrity.json")
        for changes in ({"decision_id": "f" * 32}, {"snapshot_valid": False}, {"extra": "private-sentinel"},
                        {"previous_trust_digest": "private-sentinel"}, {"schema_version": 2}):
            self.runtime.store.write(name, dict(original, **changes))
            with self.subTest(fields=list(changes)), patch.object(self.runtime.backend, "observe") as observe:
                with self.assertRaises(RuntimeFailure) as caught:
                    self.runtime.apply_decision(decision_id, "adopt")
                observe.assert_not_called()
                self.assertNotIn("private-sentinel", str(caught.exception))
            self.assertEqual(self.runtime.store.read("integrity.json"), before)
        self.runtime.store.write(name, original)
        self.assertEqual(self.runtime.apply_decision(decision_id, "adopt")["status"], "completed")

    def test_invalid_trusted_record_cannot_authorize_mutation(self):
        original = self.runtime.store.read("integrity.json")
        schema = validator("integrity")
        schema.validate(original)
        for changes in ({"repository_identity": "f" * 64}, {"last_trusted_oid": "public-label"},
                        {"extra_trust": True}, {"schema_version": 2}):
            with self.subTest(changes=changes):
                self.runtime.store.write("integrity.json", dict(original, **changes))
                with patch.object(self.runtime.backend, "push", side_effect=AssertionError("Must not send")):
                    with self.assertRaises(RuntimeFailure):
                        self.runtime.acquire()
                if "repository_identity" not in changes:
                    self.assertFalse(schema.is_valid(dict(original, **changes)))
        self.runtime.store.write("integrity.json", original)
        self.assertEqual(self.runtime.acquire()["ownership"], "owned")
