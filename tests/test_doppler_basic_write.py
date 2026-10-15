import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_handoff.contracts import RuntimeFailure
from agent_handoff.secrets.doppler import Doppler


class DopplerBasicWriteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.checkout = Path(self.temp.name)
        self.adapter = Doppler("fixture-project", "dev", self.checkout)

    @staticmethod
    def _id(number="a"):
        return number * 32

    def _read_as(self, value):
        return patch.object(self.adapter, "read", return_value=value)

    def test_basic_writer_does_not_claim_strong_capabilities_or_provider_lease(self):
        self.assertTrue(self.adapter.basic_writes)
        self.assertFalse(self.adapter.capabilities.strong_writer_fencing)
        self.assertFalse(self.adapter.capabilities.conditional_key_write)
        self.assertFalse(self.adapter.capabilities.terminal_write_evidence)
        self.adapter.require_write_gate()
        self.assertIsNone(self.adapter.lease(repository_identity="repo", publication_ref="refs/heads/main",
                                             session_id=self._id(), observed_head="b" * 40))

    def test_set_uses_scoped_stdin_and_preserves_multiline_trailing_lf(self):
        secret = "first line\n second line \n"
        completed = subprocess.CompletedProcess([], 0, secret.encode(), b"private provider diagnostic")
        with self._read_as({"TOKEN": "old"}) as read, patch(
                "agent_handoff.secrets.doppler.subprocess.run", return_value=completed) as execute:
            self.assertIsNone(self.adapter.write_key("TOKEN", secret, expected="old", lease=None,
                                                     operation_id=self._id()))

        command = execute.call_args.args[0]
        options = execute.call_args.kwargs
        self.assertIn("--no-read-env", command)
        self.assertIn("--attempts", command)
        self.assertIn("1", command)
        self.assertIn("--timeout", command)
        self.assertIn("10s", command)
        self.assertIn("--silent", command)
        self.assertIn("--project", command)
        self.assertIn("fixture-project", command)
        self.assertIn("--config", command)
        self.assertIn("dev", command)
        self.assertIn("secrets", command)
        self.assertIn("set", command)
        self.assertIn("TOKEN", command)
        self.assertNotIn(secret, " ".join(command))
        self.assertEqual(options["input"], secret.encode() + b"\n")
        self.assertEqual(options["timeout"], 15)
        self.assertTrue(all(not key.startswith("DOPPLER_") for key in options["env"]))
        self.assertNotIn(secret, "\n".join(options["env"].values()))
        read.assert_called_once_with({"TOKEN"})
        self.assertEqual(self.adapter.operation_status(self._id()), "published")

    def test_delete_is_scoped_and_noninteractive(self):
        with self._read_as({"TOKEN": "old"}), patch(
                "agent_handoff.secrets.doppler.subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, b"", b"")) as execute:
            self.adapter.write_key("TOKEN", None, expected="old", lease=None, operation_id=self._id("b"))
        command = execute.call_args.args[0]
        self.assertIn("delete", command)
        self.assertIn("--yes", command)
        self.assertIn("--silent", command)
        self.assertIsNone(execute.call_args.kwargs["input"])
        self.assertEqual(self.adapter.operation_status(self._id("b")), "published")

    def test_changed_preflight_value_rejects_without_sending(self):
        with self._read_as({"TOKEN": "new"}) as read, patch(
                "agent_handoff.secrets.doppler.subprocess.run") as execute:
            with self.assertRaises(RuntimeFailure) as caught:
                self.adapter.write_key("TOKEN", "desired", expected="old", lease=None,
                                       operation_id=self._id("c"))
        self.assertEqual(caught.exception.status, "rejected")
        self.assertEqual(caught.exception.code, 4)
        read.assert_called_once_with({"TOKEN"})
        execute.assert_not_called()
        self.assertEqual(self.adapter.operation_status(self._id("c")), "rejected")

    def test_invalid_key_value_carriage_return_and_operation_id_do_not_send(self):
        invalid = (("bad-key", "value", self._id("d")),
                   ("TOKEN", 4, self._id("e")),
                   ("TOKEN", "line\rvalue", self._id("f")),
                   ("TOKEN", "value", "not-an-operation-id"))
        with patch.object(self.adapter, "read", return_value={}), patch(
                "agent_handoff.secrets.doppler.subprocess.run") as execute:
            for key, value, operation_id in invalid:
                with self.subTest(key=key, operation_id=operation_id), self.assertRaises(RuntimeFailure):
                    self.adapter.write_key(key, value, expected=None, lease=None, operation_id=operation_id)
        execute.assert_not_called()

    def test_timeout_and_nonzero_are_unknown_redacted_and_never_retried(self):
        sentinel = "BASIC_DOPPLER_PRIVATE_ERROR_SENTINEL_3d7a"
        outcomes = (
            subprocess.TimeoutExpired("doppler", 15, output=sentinel.encode(), stderr=sentinel.encode()),
            subprocess.CompletedProcess([], 1, sentinel.encode(), sentinel.encode()),
        )
        for index, outcome in enumerate(outcomes):
            operation_id = self._id(str(index + 1))
            with self.subTest(outcome=type(outcome).__name__), self._read_as({"TOKEN": "old"}), patch(
                    "agent_handoff.secrets.doppler.subprocess.run", side_effect=outcome) as execute:
                with self.assertRaises(RuntimeFailure) as caught:
                    self.adapter.write_key("TOKEN", "new", expected="old", lease=None,
                                           operation_id=operation_id)
                self.assertEqual((caught.exception.status, caught.exception.code), ("unknown", 5))
                self.assertNotIn(sentinel, str(caught.exception))
                self.assertEqual(self.adapter.operation_status(operation_id), "unknown")
                with self.assertRaises(RuntimeFailure):
                    self.adapter.write_key("TOKEN", "new", expected="old", lease=None,
                                           operation_id=operation_id)
                execute.assert_called_once()

    def test_cli_start_failure_is_rejected_before_send(self):
        operation_id = self._id("9")
        with self._read_as({}), patch("agent_handoff.secrets.doppler.subprocess.run",
                                      side_effect=FileNotFoundError("private path")) as execute:
            with self.assertRaises(RuntimeFailure) as caught:
                self.adapter.write_key("TOKEN", "value", expected=None, lease=None,
                                       operation_id=operation_id)
        self.assertEqual((caught.exception.status, caught.exception.code), ("not_ready", 6))
        self.assertNotIn("private path", str(caught.exception))
        self.assertEqual(self.adapter.operation_status(operation_id), "rejected")
        execute.assert_called_once()


if __name__ == "__main__":
    unittest.main()
