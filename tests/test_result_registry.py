"""Every registered CLI result is covered by a closed public contract."""
import contextlib
import io
import json
import unittest

from agent_handoff.cli import main
from agent_handoff.contracts import RuntimeFailure
from agent_handoff.output import result
from agent_handoff.public_results import REGISTERED_COMMANDS, validate_public_result
from test_record_schemas import SCHEMAS, validator


class ResultRegistryTests(unittest.TestCase):
    def test_registry_agrees_with_schema_and_error_envelopes_are_closed(self):
        self.assertEqual(set(SCHEMAS["result.schema.json"]["properties"]["command"]["enum"]), REGISTERED_COMMANDS)
        for command in sorted(REGISTERED_COMMANDS):
            with self.subTest(command=command):
                value = result(command, status="invalid")
                validator("result").validate(value)
                invalid = dict(value, raw="private-sentinel")
                self.assertFalse(validator("result").is_valid(invalid))
                with self.assertRaises(RuntimeFailure):
                    validate_public_result(invalid)

    def test_unknown_labels_and_generic_success_cannot_bypass_contracts(self):
        for command in ("new-command", "inspect private", "secrets export", "decision restore"):
            with self.assertRaises(RuntimeFailure):
                result(command, status="invalid")
        for command in ("unknown", "recover-ownership"):
            with self.assertRaises(RuntimeFailure):
                result(command)

    def test_invalid_cli_arguments_emit_valid_safe_generic_error(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(["unregistered-cli-command"])
        self.assertEqual(code, 2)
        value = json.loads(output.getvalue())
        self.assertEqual(value["command"], "unknown")
        self.assertFalse(value["status"] == "completed")
        validator("result").validate(value)
