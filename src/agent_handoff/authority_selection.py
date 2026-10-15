"""Durable local authority selection; never grants remote writer capability."""
from contextlib import contextmanager
import re

from .contracts import RuntimeFailure
from .journal import opaque_id, validate_id
from .locks import lock


def _digest(value):
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise RuntimeFailure("Invalid authority selection identity.")
    return value


class AuthoritySelection:
    """Caller must validate remote consent and hold both repository locks."""

    def __init__(self, store, checkout_id, context_id):
        self.store = store
        self.checkout_id = _digest(checkout_id)
        self.context_id = _digest(context_id)
        self.prefix = "selection-" + self.checkout_id + "-" + self.context_id

    @contextmanager
    def locked(self):
        with lock(self.store, self.prefix + ".lock"):
            yield

    def _validate(self, value):
        fields = {"schema_version", "checkout_identity", "context_identity", "repository_identity", "transition_id"}
        if (not isinstance(value, dict) or set(value) != fields or
                type(value["schema_version"]) is not int or value["schema_version"] != 1 or
                value["checkout_identity"] != self.checkout_id or value["context_identity"] != self.context_id):
            raise RuntimeFailure("Invalid authority selection record.")
        _digest(value["repository_identity"])
        if value["transition_id"] is not None:
            validate_id(value["transition_id"])
        return value

    def read(self):
        value = self.store.read(self.prefix + ".json", missing=True)
        return None if value is None else self._validate(value)

    def _journal(self):
        value = self.store.read(self.prefix + "-transition.json", missing=True)
        if value is None:
            return None
        if (set(value) != {"schema_version", "transition_id", "decision_id", "before", "after", "phase", "outcome"} or
                value["schema_version"] != 1 or
                value["phase"] not in ("prepared", "resolved") or
                value["outcome"] not in (None, "selected", "not_selected")):
            raise RuntimeFailure("Invalid authority transition journal.")
        validate_id(value["transition_id"])
        validate_id(value["decision_id"])
        before, after = self._validate(value["before"]), self._validate(value["after"])
        if (before["repository_identity"] == after["repository_identity"] or
                after["transition_id"] != value["transition_id"] or
                (value["phase"] == "prepared") != (value["outcome"] is None)):
            raise RuntimeFailure("Inconsistent authority transition journal.")
        return value

    def require_clear(self):
        value = self._journal()
        if value is not None and value["phase"] != "resolved":
            raise RuntimeFailure("Resolve pending authority transition before mutation.", "unknown", 5)

    def require(self, identity):
        self.require_clear()
        value = self.read()
        if value is None or value["repository_identity"] != _digest(identity):
            raise RuntimeFailure("Authority selection requires explicit consent.", "decision_required", 3)
        return value

    def register(self, identity):
        self.require_clear()
        identity = _digest(identity)
        value = self.read()
        if value is not None:
            return self.require(identity)
        value = {"schema_version": 1, "checkout_identity": self.checkout_id,
                 "context_identity": self.context_id, "repository_identity": identity, "transition_id": None}
        self.store.write(self.prefix + ".json", value)
        return value

    def switch(self, source, target, decision_id, *, fault=lambda _: None, finalize=None):
        """Commit an already verified decision under the caller's selection lock."""
        before = self.require(source)
        target = _digest(target)
        validate_id(decision_id)
        if source == target:
            raise RuntimeFailure("Authority transition requires distinct identities.")
        transition_id = opaque_id()
        after = dict(before, repository_identity=target, transition_id=transition_id)
        journal = {"schema_version": 1, "transition_id": transition_id, "decision_id": decision_id,
                   "before": before, "after": after, "phase": "prepared", "outcome": None}
        self.store.write(self.prefix + "-transition.json", journal)
        fault("after_transition_journal")
        self.store.write(self.prefix + ".json", after)
        fault("after_selection_commit")
        return self.resolve(finalize=finalize, fault=fault)

    def resolve(self, *, finalize=None, fault=lambda _: None):
        """Observe the local commit point; never replays a selection write."""
        journal = self._journal()
        if journal is None:
            raise RuntimeFailure("No authority transition journal exists.")
        if journal["phase"] == "resolved":
            expected = journal["after" if journal["outcome"] == "selected" else "before"]
            if self.read() != expected:
                raise RuntimeFailure("Terminal authority selection evidence changed.", "unknown", 5)
            if finalize is not None:
                finalize(journal, journal["outcome"])
            return journal
        actual = self.read()
        if actual == journal["after"]:
            outcome = "selected"
        elif actual == journal["before"]:
            outcome = "not_selected"
        else:
            raise RuntimeFailure("Authority selection cannot be proven.", "unknown", 5)
        if finalize is not None:
            finalize(journal, outcome)
        fault("after_transition_finalize")
        journal = dict(journal, phase="resolved", outcome=outcome)
        self.store.write(self.prefix + "-transition.json", journal)
        return journal
