"""Scoped Doppler CLI boundary; secret values and provider diagnostics stay private."""
import json
import os
from pathlib import Path
import subprocess
import re

from ..contracts import RuntimeFailure, _unique_pairs
from .adapter import Adapter, AuthenticationRequired
from .dotenv import KEY


class Doppler(Adapter):
    # Basic CLI writes are supported. These do not prove provider-side fencing,
    # conditional writes, or durable terminal outcomes.
    basic_writes = True

    def __init__(self, project: str, config: str, checkout: Path):
        super().__init__(project, config)
        self.checkout = checkout
        self.version_verified = False
        self._operation_statuses = {}

    def require_write_gate(self):
        if not self.basic_writes:
            super().require_write_gate()

    def lease(self, *, repository_identity: str, publication_ref: str, session_id: str, observed_head: str):
        self.require_write_gate()
        # Doppler CLI tokens are config scoped only; Git ownership is not a
        # provider-checked lease or fencing token.
        return None

    @staticmethod
    def _write_id(operation_id):
        return isinstance(operation_id, str) and re.fullmatch(r"[0-9a-f]{32}", operation_id) is not None

    def write_key(self, key: str, value: str | None, *, expected: str | None, lease, operation_id: str):
        self.require_write_gate()
        if not self._write_id(operation_id):
            raise RuntimeFailure("Invalid Doppler operation identity.")
        previous = self._operation_statuses.get(operation_id)
        if previous == "published":
            return
        if previous == "rejected":
            raise RuntimeFailure("Doppler write was rejected before sending.", "rejected", 4)
        if previous == "unknown":
            raise RuntimeFailure("Doppler write outcome is unknown; reconcile fresh state before retrying.", "unknown", 5)

        self._operation_statuses[operation_id] = "rejected"
        if not isinstance(key, str) or KEY.fullmatch(key) is None:
            raise RuntimeFailure("Invalid Doppler secret key.")
        if value is not None and not isinstance(value, str):
            raise RuntimeFailure("Invalid Doppler secret value.")
        if not isinstance(expected, (str, type(None))):
            raise RuntimeFailure("Invalid expected Doppler value.")
        if value is not None and "\r" in value:
            # The CLI's line scanner removes carriage returns; refuse lossy writes.
            raise RuntimeFailure("This Doppler CLI write path cannot preserve carriage returns.", "unsupported", 8)
        try:
            encoded_value = value.encode("utf-8", "strict") if value is not None else None
        except UnicodeEncodeError:
            raise RuntimeFailure("Doppler secret value is not valid UTF-8.") from None

        try:
            current = self.read({key})
        except Exception:
            raise RuntimeFailure("Doppler preflight read failed; no write was sent.", "not_ready", 6) from None
        if not isinstance(current, dict) or any(not isinstance(item, str) for item in current.values()):
            raise RuntimeFailure("Doppler preflight read was invalid; no write was sent.", "not_ready", 6)
        if (key in current) != (expected is not None) or current.get(key) != expected:
            raise RuntimeFailure("Doppler value changed since the reviewed observation.", "rejected", 4)

        arguments = ["--no-check-version", "--no-read-env", "--attempts", "1", "--timeout", "10s",
                     "--silent", "secrets"]
        if value is None:
            arguments.extend(["delete", key, "--yes"])
            input_value = None
        else:
            arguments.extend(["set", key, "--no-interactive"])
            # Doppler's stdin parser joins scanned lines with LF. A second
            # terminal LF preserves a value whose last character is LF.
            input_value = encoded_value
            if value.endswith("\n"):
                input_value += b"\n"
        arguments.extend(["--project", self.project, "--config", self.config])
        env = {key: item for key, item in os.environ.items() if not key.startswith("DOPPLER_")}

        # A timeout or nonzero result may follow server acceptance. Never retry
        # automatically; the service must reconcile a fresh scoped observation.
        self._operation_statuses[operation_id] = "unknown"
        try:
            response = subprocess.run(["doppler", *arguments], cwd=self.checkout, env=env,
                                      input=input_value, capture_output=True, timeout=15)
        except OSError:
            self._operation_statuses[operation_id] = "rejected"
            raise RuntimeFailure("Doppler CLI could not start; no write was sent.", "not_ready", 6) from None
        except subprocess.TimeoutExpired:
            raise RuntimeFailure("Doppler write timed out; reconcile fresh state before retrying.", "unknown", 5) from None
        except Exception:
            raise RuntimeFailure("Doppler write outcome is unknown; reconcile fresh state before retrying.", "unknown", 5) from None
        if response.returncode != 0:
            raise RuntimeFailure("Doppler write outcome is unknown; reconcile fresh state before retrying.", "unknown", 5)
        self._operation_statuses[operation_id] = "published"

    def operation_status(self, operation_id: str) -> str:
        if not self._write_id(operation_id):
            return "unknown"
        return self._operation_statuses.get(operation_id, "unknown")

    def _run(self, arguments):
        env = {key: value for key, value in os.environ.items() if not key.startswith("DOPPLER_")}
        try:
            response = subprocess.run(["doppler", *arguments], cwd=self.checkout, env=env, capture_output=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            raise RuntimeFailure("Doppler authentication or scoped read is unavailable.", "not_ready", 6) from None
        if response.returncode:
            if (self._is_approved_json_read(arguments) and response.stdout == b"" and
                    self._is_missing_token_json(response.stderr)):
                raise AuthenticationRequired() from None
            raise RuntimeFailure("Doppler scoped read failed; inspect authentication locally.", "not_ready", 6)
        return response.stdout

    def _is_approved_json_read(self, arguments):
        args = list(arguments)
        prefix = ["--no-check-version", "--no-read-env", "--attempts", "1", "--timeout", "10s",
                  "secrets", "get"]
        suffix = ["--json", "--no-exit-on-missing-secret", "--project", self.project,
                  "--config", self.config]
        if args[:len(prefix)] != prefix or len(args) <= len(prefix) + len(suffix):
            return False
        keys = args[len(prefix):-len(suffix)]
        return (args[-len(suffix):] == suffix and len(keys) == len(set(keys)) and
                {"DOPPLER_PROJECT", "DOPPLER_CONFIG"} <= set(keys) and
                all(KEY.fullmatch(key) for key in keys))

    @staticmethod
    def _is_missing_token_json(stderr):
        if not isinstance(stderr, bytes) or len(stderr) > 4096:
            return False
        try:
            value = json.loads(stderr.decode("utf-8"), object_pairs_hook=_unique_pairs)
        except (ValueError, UnicodeError, RecursionError, RuntimeFailure):
            return False
        return (isinstance(value, dict) and set(value) == {"error"} and
                isinstance(value["error"], str) and value["error"].strip().casefold() == "you must provide a token")

    def read(self, scope: set[str]) -> dict[str, str]:
        if any(not isinstance(key, str) or not KEY.fullmatch(key) for key in scope):
            raise RuntimeFailure("Invalid Doppler read scope.")
        if not self.version_verified:
            version = self._run(["--version"])
            match = re.search(rb"\bv?(\d+)\.(\d+)\.(\d+)\b", version)
            if match is None or tuple(int(number) for number in match.groups()) < (3, 58, 0):
                raise RuntimeFailure("Doppler CLI 3.58.0+ is required to distinguish restricted from empty values.", "unsupported", 8)
            self.version_verified = True
        # Request only approved keys and the two non-secret identity facts.
        arguments = ["--no-check-version", "--no-read-env", "--attempts", "1", "--timeout", "10s",
                     "secrets", "get", *sorted(scope | {"DOPPLER_PROJECT", "DOPPLER_CONFIG"}),
                     "--json", "--no-exit-on-missing-secret", "--project", self.project, "--config", self.config]
        raw = self._run(arguments)
        if len(raw) > 8 * 1024 * 1024:
            raise RuntimeFailure("Doppler response exceeds the supported size.", "unsupported", 8)
        try:
            values = json.loads(raw.decode(), object_pairs_hook=_unique_pairs)
        except (ValueError, UnicodeError, RecursionError):
            raise RuntimeFailure("Invalid private Doppler response.") from None
        if not isinstance(values, dict):
            raise RuntimeFailure("Unsupported Doppler download response.")
        computed = {}
        for key, value in values.items():
            if key not in scope | {"DOPPLER_PROJECT", "DOPPLER_CONFIG"}:
                raise RuntimeFailure("Doppler returned keys outside the approved request scope.", "rejected", 4)
            if isinstance(value, dict):
                value = value.get("computed")
            if not isinstance(value, str):
                raise RuntimeFailure("A scoped Doppler value is restricted or unavailable.", "not_ready", 6)
            computed[key] = value
        values = computed
        if values.get("DOPPLER_PROJECT") != self.project or values.get("DOPPLER_CONFIG") != self.config:
            raise RuntimeFailure("Doppler response destination does not match the approved binding.", "rejected", 4)
        return {key: values[key] for key in scope if key in values and not key.startswith("DOPPLER_")}
