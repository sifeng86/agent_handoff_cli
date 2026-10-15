"""Public authority result contracts cannot carry provider grants or capabilities."""
import contextlib
import io
import json
from unittest.mock import patch

from agent_handoff.authority import Authority
from agent_handoff.authority_transition import AuthorityTransition
from agent_handoff.cli import main
from agent_handoff.contracts import RuntimeFailure
from agent_handoff.output import result
from agent_handoff.paths import PrivateStore
from agent_handoff.public_results import validate_authority_result
from agent_handoff.publication import Runtime
from test_publication import RepositoryFixture
from test_record_schemas import validator


class AuthorityPublicTests(RepositoryFixture):
    def proposal(self):
        with Runtime(self.checkout, self.root / "private", Authority(str(self.remote), "refs/heads/next"), self.context) as target:
            return AuthorityTransition(self.runtime, target).propose()

    def test_real_proposal_and_selection_results_match_public_schemas(self):
        proposal = self.proposal()
        samples = [proposal, result("authority apply"), result("authority", status="invalid")]
        for command, outcome in (("authority apply", "selected"), ("authority resolve", "not_selected")):
            samples.append(result(command, transition_id="a" * 32, transition_outcome=outcome,
                                  selected_authority_identity="b" * 64))
        for value in samples:
            validate_authority_result(value)
            validator("authority-result").validate(value)
            validator("result").validate(value)
            for changes in ({"raw_base64": "private-sentinel"}, {"secrets_shared": True}, {"ownership": "owned"},
                            {"operation_id": "f" * 32}, {"published_revision": "f" * 40}, {"schema_version": True}):
                invalid = dict(value, **changes)
                self.assertFalse(validator("authority-result").is_valid(invalid))
                with self.assertRaises(RuntimeFailure):
                    validate_authority_result(invalid)

    def test_wrong_decision_or_partial_selection_evidence_refused(self):
        proposal = self.proposal()
        for changes in ({"category": "secret_conflict"}, {"upload_authorized": True}, {"observed_head": None}):
            invalid = dict(proposal, decision=dict(proposal["decision"], **changes))
            self.assertFalse(validator("authority-result").is_valid(invalid))
            with self.assertRaises(RuntimeFailure):
                validate_authority_result(invalid)
        partial = dict(result("authority apply"), transition_id="a" * 32)
        self.assertFalse(validator("authority-result").is_valid(partial))
        with self.assertRaises(RuntimeFailure):
            validate_authority_result(partial)
        same = dict(proposal, decision=dict(proposal["decision"], destination_identity=proposal["decision"]["repository_identity"]))
        with self.assertRaises(RuntimeFailure):
            validate_authority_result(same)

    def test_cli_final_serializer_suppresses_mutated_authority_payload(self):
        with PrivateStore(self.root / "private", self.checkout) as store:
            store.write("context.json", {"schema_version": 1, "context": self.context})
        output = io.StringIO()
        original = AuthorityTransition.propose
        def unsafe(transition):
            value = original(transition)
            value.update(command="inspect", raw_base64="private-sentinel")
            return value
        with patch.object(AuthorityTransition, "propose", unsafe), contextlib.redirect_stdout(output):
            code = main(["authority", "propose", "--checkout", str(self.checkout), "--private-root", str(self.root / "private"),
                         "--context-file", str(self.root / "private/context.json"), "--authority", str(self.remote),
                         "--publication-ref", self.authority.publication_ref, "--target-authority", str(self.remote),
                         "--target-publication-ref", "refs/heads/next"])
        self.assertEqual(code, 7)
        self.assertNotIn("private-sentinel", output.getvalue())
        public = json.loads(output.getvalue())
        self.assertEqual(public["command"], "authority")
        validator("authority-result").validate(public)
