import copy
import json
from pathlib import Path
import unittest

from jsonschema import Draft202012Validator, FormatChecker, RefResolver

from agent_handoff.contracts import RuntimeFailure
from agent_handoff.record_checks import config, decision, decision_state, journal, session
from agent_handoff.secrets.manifest import parse as parse_manifest
from agent_handoff.plans import parse as parse_plan
from test_publication import RepositoryFixture


ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = {path.name: json.loads(path.read_text()) for path in (ROOT / "schemas").glob("*.schema.json")}
FIXTURES = ROOT / "tests/fixtures/records"


def validator(name):
    schema = SCHEMAS[name + ".schema.json"]
    Draft202012Validator.check_schema(schema)
    base = (ROOT / "schemas").as_uri() + "/"
    store = {base + filename: value for filename, value in SCHEMAS.items()}
    return Draft202012Validator(schema, resolver=RefResolver(base, schema, store=store), format_checker=FormatChecker())


class SchemaFixturesTests(unittest.TestCase):
    def test_all_schema_documents_are_valid(self):
        for name, schema in SCHEMAS.items():
            with self.subTest(schema=name):
                Draft202012Validator.check_schema(schema)

    def test_private_valid_invalid_fixtures_agree_with_runtime(self):
        for path in FIXTURES.glob("*.json"):
            fixture = json.loads(path.read_text())
            with self.subTest(fixture=path.name):
                schema = validator(fixture["schema"])
                self.assertEqual(schema.is_valid(fixture["record"]), fixture["valid"])
                checks = {"git-journal": journal, "decision": decision, "decision-state": decision_state,
                          "config": config, "plan": lambda record: parse_plan("<!-- agent-handoff-plan\n" + json.dumps(record) + "\n-->\n"),
                          "secrets-manifest": lambda record: parse_manifest(json.dumps(record)),
                          "session": lambda record: session(record, active=True), "session-candidate": session}
                check = checks[fixture["schema"]]
                if fixture["valid"]:
                    check(fixture["record"])
                else:
                    with self.assertRaises(RuntimeFailure):
                        check(fixture["record"])

    def test_templates_and_public_result_fixtures(self):
        from agent_handoff.output import result
        for name in ("protocol", "owner", "state"):
            value = json.loads((ROOT / "template/.agent" / (name + ".json")).read_text())
            with self.subTest(schema=name):
                schema = validator(name)
                schema.validate(value)
                schema_version = dict(value, schema_version=True)
                self.assertFalse(schema.is_valid(schema_version))
                self.assertFalse(schema.is_valid(dict(value, schema_version=2)))
                invalid = dict(value)
                invalid.pop("schema_version")
                self.assertFalse(schema.is_valid(invalid))
        value = result("inspect", inspection={"observed_revision": "a" * 40, "owner_present": False,
                                               "runtime_status": "idle", "protocol_version": "1.2"})
        validator("plan").validate(parse_plan((ROOT / "template/plans/P-001-example.md").read_text()))
        schema = validator("result")
        schema.validate(value)
        self.assertFalse(schema.is_valid(dict(value, ownership="owned-by-id-only")))
        patch = {"schema_version": 1, "expected": {"A": "dummy"}, "changes": {"A": None}, "approved_deletions": ["A"]}
        schema = validator("local-secret-patch")
        schema.validate(patch)
        for changes in ({"expected": {"A": None}}, {"changes": {"A": 123}}, {"approved_deletions": ["A", "A"]}):
            self.assertFalse(schema.is_valid(dict(patch, **changes)))
        material_patch = dict(patch, expected_files={"CERT": None}, file_changes={"CERT": "dummy"})
        schema.validate(material_patch)
        self.assertFalse(schema.is_valid(dict(patch, expected_files={"CERT": None})))
        self.assertFalse(schema.is_valid(dict(patch, file_changes={"CERT": "dummy"})))


class RuntimeSchemaTests(RepositoryFixture):
    def test_real_acquisition_journal_and_active_session_match_schemas(self):
        report = self.runtime.acquire()
        record = self.runtime.journal.read(report["operation_id"])
        validator("git-journal").validate(record)
        validator("session").validate(self.runtime.sessions.current())
        validator("config").validate(self.runtime.backend.records(report["published_revision"])["config"])
        corrupt = copy.deepcopy(record)
        corrupt["candidate"]["session_id"] = "f" * 32
        with self.assertRaises(RuntimeFailure):
            self.runtime.journal.write(corrupt)

    def test_invalid_journal_blocks_mutation_without_network_send(self):
        from unittest.mock import patch
        report = self.runtime.acquire()
        record = self.runtime.journal.read(report["operation_id"])
        corrupt = dict(record, phase="resolved", result="publication_unknown")
        self.runtime.store.write("operation-" + record["operation_id"] + ".json", corrupt)
        with patch.object(self.runtime.backend, "push", side_effect=AssertionError("Must not send")):
            with self.assertRaises(RuntimeFailure):
                self.runtime.release()

    def test_journal_filename_must_match_its_operation_identity(self):
        report = self.runtime.acquire()
        record = self.runtime.journal.read(report["operation_id"])
        copied = copy.deepcopy(record)
        copied["operation_id"] = "f" * 32
        copied["candidate"]["acquisition_operation_id"] = "f" * 32
        self.runtime.store.write("operation-" + record["operation_id"] + ".json", copied)
        with self.assertRaises(RuntimeFailure):
            self.runtime.journal.pending()
