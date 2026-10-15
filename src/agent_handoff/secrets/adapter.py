"""Trusted backend interface. Capability gates cannot be enabled by repo config."""
from dataclasses import dataclass
import hashlib
import json
import re

from ..contracts import RuntimeFailure


class SharedReadUnavailable(RuntimeFailure):
    """A trusted provider read failed without exposing provider diagnostics."""

    def __init__(self, status="not_ready", code=6):
        super().__init__("The approved shared secret read is unavailable.", status, code)


class AuthenticationRequired(SharedReadUnavailable):
    """The approved shared read is blocked by a verified missing-token response."""

    def __init__(self):
        RuntimeFailure.__init__(self, "Doppler authentication is required for the approved read.", "not_ready", 6)


def destination_identity(mode: str, project: str | None, config: str | None) -> str:
    if mode not in ("local-env", "doppler"):
        raise RuntimeFailure("An explicit supported secrets mode is required.")
    if mode == "local-env":
        if project is not None or config is not None:
            raise RuntimeFailure("Local-env mode has no shared destination.")
    elif any(not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", name) for name in (project, config)):
        raise RuntimeFailure("Explicit Doppler project and config names are required.")
    return hashlib.sha256(json.dumps([1, mode, project, config], separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class Capabilities:
    scoped_read: bool = True
    strong_writer_fencing: bool = False
    conditional_key_write: bool = False
    terminal_write_evidence: bool = False


class Adapter:
    """Implementations are trusted runtime code, never repository executors.

    The default gate requires verified provider receipts and fencing. Trusted
    basic providers may explicitly implement their simpler write contract;
    the service then confirms current state without claiming terminal receipts.
    """
    capabilities = Capabilities()

    def __init__(self, project: str, config: str):
        self.project = project
        self.config = config
        self.destination_identity = destination_identity("doppler", project, config)

    def read(self, scope: set[str]) -> dict[str, str]:
        raise NotImplementedError

    def require_write_gate(self):
        capabilities = self.capabilities
        if not (capabilities.strong_writer_fencing and capabilities.conditional_key_write and capabilities.terminal_write_evidence):
            raise RuntimeFailure("Shared upload is unsupported until provider fencing, conditional writes and terminal outcomes are verified.", "unsupported", 8)

    def lease(self, *, repository_identity: str, publication_ref: str, session_id: str, observed_head: str):
        self.require_write_gate()
        raise NotImplementedError

    def write_key(self, key: str, value: str | None, *, expected: str | None, lease, operation_id: str):
        self.require_write_gate()
        raise NotImplementedError

    def operation_status(self, operation_id: str) -> str:
        return "unknown"
