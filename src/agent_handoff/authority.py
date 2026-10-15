"""Explicit canonical authority and durable local approval binding."""
from dataclasses import dataclass
import hashlib
import json

from .contracts import RuntimeFailure
from .git_backend import validate_destination
from .paths import PrivateStore


@dataclass(frozen=True)
class Authority:
    destination: str
    publication_ref: str

    def __post_init__(self):
        object.__setattr__(self, "destination", validate_destination(self.destination))
        if not self.publication_ref.startswith("refs/heads/"):
            raise RuntimeFailure("Publication ref must be a full branch ref.")

    @property
    def identity(self) -> str:
        encoding = json.dumps([1, self.destination, self.publication_ref], separators=(",", ":"))
        return hashlib.sha256(encoding.encode()).hexdigest()

    def bind(self, store: PrivateStore):
        expected = {"schema_version": 1, "repository_identity": self.identity,
                    "destination": self.destination, "publication_ref": self.publication_ref}
        previous = store.read("bindings.json", missing=True)
        if previous is not None and previous != expected:
            raise RuntimeFailure("Authority binding changed; explicit authority decision is required.", "decision_required", 3)
        if previous is None:
            store.write("bindings.json", expected)

    def require_binding(self, store: PrivateStore):
        value = store.read("bindings.json")
        if value != {"schema_version": 1, "repository_identity": self.identity,
                     "destination": self.destination, "publication_ref": self.publication_ref}:
            raise RuntimeFailure("Authority does not match the approved binding.", "decision_required", 3)
