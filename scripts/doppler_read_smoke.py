"""Live scoped-read acceptance using disposable local Git fixtures."""
import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from agent_handoff.secrets.doppler import Doppler
from agent_handoff.secrets.service import SecretService
from test_publication import RepositoryFixture


def run(project, config, key, mapping=None):
    results = []
    for case in ("A41", "A42"):
        fixture = RepositoryFixture()
        try:
            fixture.setUp()
            fixture.runtime.acquire()
            adapter = Doppler(project, config, fixture.checkout)
            manifest = {"schema_version": 1, "keys": {
                key: {"classification": "file-backed" if mapping else "shared", "required": True}}}
            if mapping:
                manifest["keys"][key]["file"] = mapping
            service = SecretService(fixture.runtime, manifest, adapter)
            values = adapter.read({key})
            if key not in values or not values[key]:
                raise ValueError("A nonempty isolated test key is required.")
            if case == "A42":
                if mapping:
                    material = fixture.checkout / mapping
                    material.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    material.write_text(values[key], encoding="ascii")
                    material.chmod(0o600)
                path = fixture.checkout / ".env"
                path.write_text(key + "=" + json.dumps(mapping or values[key]) + "\n", encoding="utf-8")
                path.chmod(0o600)
            proposal = service.propose_binding("doppler", project=project, config=config, upload_authorized=False)
            approved = service.apply_decision(proposal["decision"]["decision_id"], "approve-secret-binding")
            planned = service.reconcile()
            applied = service.apply(planned["plan_id"])
            verified = service.verify()
            if (applied["secrets_shared"] is not True or verified["secrets_shared"] is not True or
                    service._baseline(service._binding()).read() != values or
                    service.environment.read().values != ({key: mapping} if mapping else values) or
                    service._local(service.environment.read()) != values or
                    values[key] in json.dumps([proposal, approved, planned, applied, verified])):
                raise ValueError("Live acceptance verification failed.")
            results.append({"acceptance_id": case, "live_scoped_read": True,
                            "local_material_verified": True, "baseline_verified": True,
                            "public_value_leak": False, "remote_write_attempted": False})
        finally:
            fixture.doCleanups()
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--file-mapping", help="Approved relative mapping for a text PEM test key")
    args = parser.parse_args()
    try:
        report = {"passed": run(args.project, args.config, args.key, args.file_mapping)}
    except Exception:
        # Do not expose provider responses, private values or exception arguments.
        print(json.dumps({"passed": [], "error": "Live scoped-read smoke failed; inspect setup locally."}))
        return 1
    print(json.dumps(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
