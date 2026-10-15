"""Integrity decisions use the same protected serializer as other results."""
import contextlib
import io
import json
from unittest.mock import patch

from agent_handoff.cli import main
from agent_handoff.contracts import RuntimeFailure
from agent_handoff.paths import PrivateStore
from agent_handoff.public_results import validate_control_result
from agent_handoff.publication import Runtime
from test_publication import RepositoryFixture
from test_record_schemas import validator


class ControlPublicTests(RepositoryFixture):
    def test_real_integrity_proposal_and_adoption_have_no_grants_or_publication(self):
        proposal = self.runtime.propose_integrity()
        applied = self.runtime.apply_decision(proposal["decision"]["decision_id"], "adopt")
        for value in (proposal, applied):
            validate_control_result(value)
            validator("control-result").validate(value)
            validator("result").validate(value)
            for changes in ({"raw": "private-sentinel"}, {"ownership": "owned"},
                            {"secrets_shared": True}, {"code_publication": "published"}):
                invalid = dict(value, **changes)
                self.assertFalse(validator("control-result").is_valid(invalid))
                with self.assertRaises(RuntimeFailure):
                    validate_control_result(invalid)

    def test_wrong_scope_or_restore_option_refused(self):
        proposal = self.runtime.propose_integrity()
        unknown = dict(proposal, command="decision restore")
        with self.assertRaises(RuntimeFailure):
            validate_control_result(unknown)
        self.assertFalse(validator("result").is_valid(unknown))
        for changes in ({"category": "secret_conflict"}, {"upload_authorized": True},
                        {"options": [{"option_id": "restore", "effect": "Unsupported restoration"}]}):
            value = dict(proposal, decision=dict(proposal["decision"], **changes))
            self.assertFalse(validator("control-result").is_valid(value))
            with self.assertRaises(RuntimeFailure):
                validate_control_result(value)

    def test_proposal_no_longer_bypasses_final_serializer(self):
        with PrivateStore(self.root / "private", self.checkout) as store:
            store.write("context.json", {"schema_version": 1, "context": self.context})
        original = Runtime.propose_integrity
        def unsafe(runtime):
            value = original(runtime)
            value.update(command="inspect", raw_base64="private-sentinel")
            return value
        output = io.StringIO()
        with patch.object(Runtime, "propose_integrity", unsafe), contextlib.redirect_stdout(output):
            code = main(["decision", "propose-integrity", "--checkout", str(self.checkout), "--private-root", str(self.root / "private"),
                         "--context-file", str(self.root / "private/context.json"), "--authority", str(self.remote),
                         "--publication-ref", self.authority.publication_ref])
        self.assertEqual(code, 7)
        self.assertNotIn("private-sentinel", output.getvalue())
        public = json.loads(output.getvalue())
        self.assertEqual(public["command"], "decision")
        validator("control-result").validate(public)
