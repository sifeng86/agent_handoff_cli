"""Last verified convergence is sensitive, private and configuration-bound."""
from ..contracts import RuntimeFailure
from .dotenv import KEY
from ..record_checks import shape, match, ID, IDENTITY


class Baseline:
    def __init__(self, store, repository_identity: str, config_identity: str, scope: set[str]):
        if (not match(repository_identity, IDENTITY) or not match(config_identity, IDENTITY) or
                not isinstance(scope, set) or any(not isinstance(key, str) or not KEY.fullmatch(key) for key in scope)):
            raise RuntimeFailure("Invalid private baseline scope or binding.")
        self.store = store
        self.repository_identity = repository_identity
        self.config_identity = config_identity
        self.scope = scope
        self.name = "secret-baseline-" + config_identity + ".json"

    def read(self) -> dict | None:
        value = self.store.read(self.name, missing=True)
        if value is None:
            return None
        shape(value, {"schema_version", "repository_identity", "config_identity", "shared_scope", "values", "verified_sync_id"})
        if (not match(value["repository_identity"], IDENTITY) or not match(value["config_identity"], IDENTITY) or
                not match(value["verified_sync_id"], ID)):
            raise RuntimeFailure("Invalid private baseline identity.")
        if (value.get("repository_identity") != self.repository_identity or value.get("config_identity") != self.config_identity or
                value.get("shared_scope") != sorted(self.scope) or not isinstance(value.get("values"), dict)):
            raise RuntimeFailure("Secret baseline binding differs from approved scope.", "decision_required", 3)
        if not value["values"].keys() <= self.scope or any(not isinstance(v, str) for v in value["values"].values()):
            raise RuntimeFailure("Invalid private secret baseline.")
        return value["values"]

    def verified(self, values: dict, sync_id: str):
        if (not match(sync_id, ID) or not isinstance(values, dict) or not values.keys() <= self.scope or
                any(not isinstance(k, str) or not KEY.fullmatch(k) or not isinstance(v, str) for k, v in values.items())):
            raise RuntimeFailure("Invalid verified baseline.")
        self.store.write(self.name, {"schema_version": 1, "repository_identity": self.repository_identity,
                         "config_identity": self.config_identity, "shared_scope": sorted(self.scope), "values": values,
                         "verified_sync_id": sync_id})
