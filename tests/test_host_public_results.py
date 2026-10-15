"""Public host schemas and runtime boundary reject private evidence."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_handoff.cli import main
from agent_handoff.contracts import RuntimeFailure
from agent_handoff.output import result
from agent_handoff.public_results import validate_host_result
from test_record_schemas import validator


class HostPublicResultTests(unittest.TestCase):
    def samples(self):
        handle, operation = "a" * 32, "b" * 32
        inspection = {"preservation_handle": handle, "inspected_categories": ["ssh", "git"],
                      "unverifiable_categories": [], "host_changes_attempted": False,
                      "scope": "selected_linux_configuration_entries"}
        verification = {"preservation_handle": handle, "preserved_categories": ["ssh", "git"],
                        "changed_categories": [], "unverifiable_categories": [], "host_changes_attempted": False,
                        "scope": "selected_linux_configuration_entries"}
        return [result("host", status="invalid"), result("host inspect", **inspection),
                result("host verify", **verification),
                result("host backup", backup_handle=handle, backup_verified=True),
                result("host verify-backup", backup_handle=handle, backup_verified=True),
                result("host restore", backup_handle=handle, operation_id=operation, recovery_outcome="restored"),
                result("host resolve", backup_handle=handle, operation_id=operation, recovery_outcome="not_restored")]

    def test_command_contracts_agree_on_realistic_valid_and_invalid_records(self):
        for value in self.samples():
            with self.subTest(command=value["command"]):
                validate_host_result(value)
                validator("host-result").validate(value)
                validator("result").validate(value)
                for changes in ({"raw_base64": "private-sentinel"}, {"schema_version": True},
                                {"ownership": "owned"}, {"secrets_shared": True}, {"status": "ready"},
                                {"code_publication": "published"}):
                    invalid = dict(value, **changes)
                    self.assertFalse(validator("host-result").is_valid(invalid))
                    self.assertFalse(validator("result").is_valid(invalid))
                    with self.assertRaises(RuntimeFailure):
                        validate_host_result(invalid)

    def test_missing_success_fields_wrong_command_and_false_preservation_claims(self):
        backup = self.samples()[3]
        for changes in ({"backup_handle": "bad"}, {"backup_verified": False}, {"preservation_handle": "c" * 32}):
            invalid = dict(backup, **changes)
            self.assertFalse(validator("host-result").is_valid(invalid))
            with self.assertRaises(RuntimeFailure):
                validate_host_result(invalid)
        incomplete = dict(backup)
        incomplete.pop("backup_handle")
        with self.assertRaises(RuntimeFailure):
            validate_host_result(incomplete)
        self.assertFalse(validator("host-result").is_valid(incomplete))
        value = self.samples()[2]
        false_claim = dict(value, changed_categories=["ssh"])
        with self.assertRaises(RuntimeFailure):
            validate_host_result(false_claim)
        self.assertFalse(validator("host-result").is_valid(false_claim))
        with self.assertRaises(RuntimeFailure):
            validate_host_result(dict(false_claim, status="not_ready"))

    def test_cli_malformed_host_output_is_replaced_by_safe_error(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            checkout = root / "checkout"
            checkout.mkdir()
            config = root / "config"
            config.write_text("private-sentinel")
            unsafe = {"backup_handle": "a" * 32, "backup_verified": True,
                      "raw_base64": "private-sentinel", "path": str(config)}
            output = io.StringIO()
            with patch("agent_handoff.host_recovery.HostFileRecovery.backup", return_value=unsafe), \
                    contextlib.redirect_stdout(output):
                code = main(["host", "backup", "--host-file", str(config), "--checkout", str(checkout),
                             "--private-root", str(root / "private")])
            self.assertEqual(code, 7)
            self.assertNotIn("private-sentinel", output.getvalue())
            self.assertNotIn(str(config), output.getvalue())
            public = json.loads(output.getvalue())
            self.assertEqual(public["status"], "invalid")
            validator("host-result").validate(public)

    def test_final_serializer_rejects_command_identity_mutation(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            checkout = root / "checkout"
            checkout.mkdir()
            config = root / "config"
            config.write_text("private-sentinel")
            def mutated(command, **fields):
                value = result(command, **fields)
                if command == "host backup":
                    value.update(command="inspect", raw_base64="private-sentinel")
                return value
            output = io.StringIO()
            with patch("agent_handoff.cli.result", side_effect=mutated), contextlib.redirect_stdout(output):
                code = main(["host", "backup", "--host-file", str(config), "--checkout", str(checkout),
                             "--private-root", str(root / "private")])
            self.assertEqual(code, 7)
            self.assertNotIn("private-sentinel", output.getvalue())
            public = json.loads(output.getvalue())
            self.assertEqual(public["command"], "host")
            validator("host-result").validate(public)
