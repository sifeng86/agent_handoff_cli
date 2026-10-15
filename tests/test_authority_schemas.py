"""Schema contracts for real durable authority transition records."""
import copy

from agent_handoff.authority import Authority
from agent_handoff.authority_transition import AuthorityTransition
from agent_handoff.publication import Runtime
from test_publication import RepositoryFixture
from test_record_schemas import validator


class AuthoritySchemaTests(RepositoryFixture):
    def test_real_records_and_invalid_structures(self):
        with Runtime(self.checkout, self.root / "private",
                     Authority(str(self.remote), "refs/heads/next-publication"), self.context) as target:
            transition = AuthorityTransition(self.runtime, target)
            proposal = transition.propose()
            decision_id = proposal["decision"]["decision_id"]
            evidence = self.runtime.store.read("authority-decision-" + decision_id + ".json")
            validator("authority-decision").validate(evidence)
            self.runtime.fault = lambda _: (_ for _ in ()).throw(SystemExit(77))
            with self.assertRaises(SystemExit):
                transition.apply(decision_id, "switch")
            selection = self.runtime.selection
            record = selection.read()
            journal = selection._journal()
            for name, value in (("authority-selection", record), ("authority-transition", journal),
                                ("authority-decision", evidence)):
                schema = validator(name)
                schema.validate(value)
                for changes in ({"schema_version": True}, {"schema_version": 2}, {"extra": "private"}):
                    with self.subTest(schema=name, changes=list(changes)):
                        self.assertFalse(schema.is_valid(dict(value, **changes)))
                missing = dict(value)
                missing.pop("schema_version")
                self.assertFalse(schema.is_valid(missing))
            schema = validator("authority-transition")
            self.assertFalse(schema.is_valid(dict(journal, phase="resolved")))
            self.assertFalse(schema.is_valid(dict(journal, outcome="selected")))
            corrupt = copy.deepcopy(journal)
            corrupt["after"]["context_identity"] = "invalid"
            self.assertFalse(schema.is_valid(corrupt))
            corrupt = copy.deepcopy(evidence)
            corrupt["target"]["owner"] = "f" * 32
            self.assertFalse(validator("authority-decision").is_valid(corrupt))
            corrupt = copy.deepcopy(evidence)
            corrupt["source"]["head"] = None
            self.assertFalse(validator("authority-decision").is_valid(corrupt))
