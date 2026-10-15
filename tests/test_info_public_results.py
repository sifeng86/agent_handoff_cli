"""Inspection is advisory; context CLI exposes only a private file address."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_handoff.cli import main
from agent_handoff.contracts import RuntimeFailure
from agent_handoff.git_backend import inspect
from agent_handoff.output import result
from agent_handoff.public_results import validate_info_result
from test_publication import RepositoryFixture
from test_record_schemas import validator


class InspectionPublicTests(RepositoryFixture):
    def test_real_inspection_and_invalid_capability_or_private_claims(self):
        observation = inspect(self.authority.destination, self.authority.publication_ref)
        for command in ("inspect", "status"):
            value = result(command, inspection=observation)
            validator("info-result").validate(value)
            validator("result").validate(value)
            for changes in ({"ownership": "owned"}, {"code_publication": "published"},
                            {"context": "private-sentinel"}, {"operation_id": "a" * 32}):
                invalid = dict(value, **changes)
                self.assertFalse(validator("info-result").is_valid(invalid))
                with self.assertRaises(RuntimeFailure):
                    validate_info_result(invalid)
            invalid = dict(value, inspection=dict(observation, owner_present=1))
            self.assertFalse(validator("info-result").is_valid(invalid))
            with self.assertRaises(RuntimeFailure):
                validate_info_result(invalid)

    def test_cli_rejects_extra_private_inspection_fields(self):
        observed = {"observed_revision": "a" * 40, "owner_present": False,
                    "runtime_status": "idle", "protocol_version": "1.2", "raw": "private-sentinel"}
        output = io.StringIO()
        with patch("agent_handoff.cli.inspect", return_value=observed), contextlib.redirect_stdout(output):
            code = main(["inspect", "--authority", str(self.remote), "--publication-ref", self.authority.publication_ref])
        self.assertEqual(code, 7)
        self.assertNotIn("private-sentinel", output.getvalue())
        validator("info-result").validate(json.loads(output.getvalue()))


class ContextPublicTests(unittest.TestCase):
    def test_relative_private_root_returns_absolute_address_without_token(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            checkout = root / "checkout"
            checkout.mkdir()
            output = io.StringIO()
            with contextlib.chdir(root), contextlib.redirect_stdout(output):
                code = main(["context", "create", "--private-root", "private", "--checkout", str(checkout)])
            self.assertEqual(code, 0)
            value = json.loads(output.getvalue())
            path = Path(value["context_file"])
            self.assertEqual(path, root / "private/context.json")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            token = json.loads(path.read_text())["context"]
            self.assertTrue(token not in output.getvalue(), "Private context token leaked.")
            validator("info-result").validate(value)
            validator("result").validate(value)
            for changes in ({"context_file": "relative/context.json"}, {"context": "private-sentinel"}):
                invalid = dict(value, **changes)
                self.assertFalse(validator("info-result").is_valid(invalid))
                with self.assertRaises(RuntimeFailure):
                    validate_info_result(invalid)

    def test_final_inspection_identity_check_handles_nonstring_command_safely(self):
        value = result("inspect", status="invalid")
        value["command"] = {"raw": "private-sentinel"}
        with self.assertRaises(RuntimeFailure):
            validate_info_result(value, expected_command="inspect")
        value = result("context", status="invalid")
        value["command"] = "context import"
        with self.assertRaises(RuntimeFailure):
            validate_info_result(value)
        self.assertFalse(validator("result").is_valid(value))
