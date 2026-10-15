import copy
from unittest.mock import patch

from agent_handoff.contracts import RuntimeFailure
from agent_handoff.secrets.service import SecretService
from test_publication import RepositoryFixture
from test_record_schemas import validator


class DecisionRecordTests(RepositoryFixture):
    def test_A32_A33_project_semantics_cannot_enter_runtime_decisions(self):
        before = set(self.runtime.store.names())
        for category in ("test_bug", "product_feature"):
            with self.subTest(category=category), self.assertRaises(RuntimeFailure):
                self.runtime.decisions.create(category, None, None,
                    checkout_identity=self.runtime.sessions.checkout_id, context_identity=self.runtime.sessions.context_id,
                    reason="Project semantic choice", options=[{"option_id": "approve", "effect": "Change project code"}])
            self.assertEqual(set(self.runtime.store.names()), before)

    def test_secret_approval_details_are_identical_to_persisted_envelope(self):
        manifest = {"schema_version": 1, "keys": {
            "A": {"classification": "shared", "required": True}}}
        service = SecretService(self.runtime, manifest)
        proposal = service.propose_binding("local-env")
        envelope = proposal["decision"]
        self.assertEqual(self.runtime.decisions.read(envelope["decision_id"]), envelope)
        self.assertEqual(envelope["secret_manifest"], manifest)
        self.assertIs(envelope["upload_authorized"], False)
        validator("decision").validate(envelope)
        validator("decision-state").validate(self.runtime.decisions.require_pending(envelope["decision_id"]))
        service.apply_decision(envelope["decision_id"], "approve-secret-binding")

    def takeover(self):
        self.runtime.acquire()
        other = self.new_runtime("other", "b" * 64)
        self.addCleanup(other.close)
        other.initialize()
        return other, other.acquire()["decision"]

    def test_corrupt_envelope_cannot_execute_takeover(self):
        other, envelope = self.takeover()
        corrupt = copy.deepcopy(envelope)
        corrupt["options"].append(copy.deepcopy(corrupt["options"][0]))
        other.store.write("decision-" + envelope["decision_id"] + ".json", corrupt)
        before = self.observe()
        with patch.object(other.backend, "push", side_effect=AssertionError("Must not publish")):
            with self.assertRaises(RuntimeFailure):
                other.apply_decision(envelope["decision_id"], "takeover")
        self.assertEqual(self.observe(), before)

    def test_private_candidate_scope_change_cannot_reuse_public_consent(self):
        manifest = {"schema_version": 1, "keys": {"A": {"classification": "shared", "required": True}}}
        service = SecretService(self.runtime, manifest)
        proposal = service.propose_binding("local-env")
        decision_id = proposal["decision"]["decision_id"]
        name = "secret-decision-" + decision_id + ".json"
        private = self.runtime.store.read(name)
        private["candidate"]["manifest"]["keys"]["B"] = {"classification": "shared", "required": False}
        self.runtime.store.write(name, private)
        changed = SecretService(self.runtime, private["candidate"]["manifest"])
        with self.assertRaises(RuntimeFailure) as stale:
            changed.apply_decision(decision_id, "approve-secret-binding")
        self.assertEqual(stale.exception.status, "decision_required")
        self.assertIsNone(self.runtime.store.read("secret-binding.json", missing=True))

    def test_unknown_lifecycle_version_cannot_be_finished_or_applied(self):
        other, envelope = self.takeover()
        name = "decision-state-" + envelope["decision_id"] + ".json"
        lifecycle = other.store.read(name)
        other.store.write(name, dict(lifecycle, schema_version=2))
        with self.assertRaises(RuntimeFailure):
            other.decisions.finish(envelope["decision_id"], "applied")
        with self.assertRaises(RuntimeFailure):
            other.apply_decision(envelope["decision_id"], "takeover")
        with self.assertRaises(RuntimeFailure):
            other.store.read(name)
