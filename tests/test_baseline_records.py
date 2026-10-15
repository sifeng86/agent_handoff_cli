import unittest

from agent_handoff.contracts import RuntimeFailure
from agent_handoff.secrets.baseline import Baseline
from test_record_schemas import validator


class MemoryStore:
    def __init__(self):
        self.value = None

    def read(self, name, missing=False):
        return self.value

    def write(self, name, value):
        self.value = value


class BaselineRecordTests(unittest.TestCase):
    def test_corrupt_baseline_is_never_trusted(self):
        store = MemoryStore()
        baseline = Baseline(store, "a" * 64, "b" * 64, {"A"})
        baseline.verified({"A": "dummy-test-value"}, "c" * 32)
        original = dict(store.value)
        schema = validator("secret-baseline")
        schema.validate(original)
        for changes in [{"schema_version": 2}, {"schema_version": True}, {"auto_upload": True},
                        {"verified_sync_id": "public-label"}, {"values": {"A": None}}]:
            with self.subTest(changes=changes):
                store.value = dict(original, **changes)
                self.assertFalse(schema.is_valid(store.value))
                with self.assertRaises(RuntimeFailure):
                    baseline.read()
        store.value = original
        self.assertEqual(baseline.read(), {"A": "dummy-test-value"})
