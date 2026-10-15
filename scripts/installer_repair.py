"""Repair only a backed-up agentctl launcher shebang in an explicit prefix."""
import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_handoff.contracts import RuntimeFailure
from agent_handoff.installer_repair import repair_entrypoint
from agent_handoff.output import exit_code, result
from agent_handoff.paths import PrivateStore
from agent_handoff.public_results import validate_host_result


def _emit(value):
    validate_host_result(value, require_host=True)
    sys.stdout.write(json.dumps(value, ensure_ascii=True, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def run(prefix, known_good_handle, private_root, checkout):
    prefix, private_root, checkout = Path(prefix), Path(private_root), Path(checkout)
    if not all(path.is_absolute() for path in (prefix, private_root, checkout)):
        raise RuntimeFailure("Installer repair paths must be explicit absolute paths.", "rejected", 4)
    prefix_real, checkout_real = prefix.resolve(), checkout.resolve()
    if prefix_real == checkout_real or prefix_real.is_relative_to(checkout_real) or checkout_real.is_relative_to(prefix_real):
        raise RuntimeFailure("Installer prefix and project checkout must be separate.", "rejected", 4)
    emitted = []
    with PrivateStore(private_root / "host", checkout) as store:
        reports = repair_entrypoint(store, prefix, known_good_handle, emit=lambda value: (emitted.append(value), _emit(value)))
    for value in reports[len(emitted):]:
        _emit(value)
    return exit_code(reports[-1])


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--prefix", required=True, type=Path)
    parser.add_argument("--known-good-handle", required=True)
    parser.add_argument("--private-root", required=True, type=Path)
    parser.add_argument("--checkout", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        return run(args.prefix, args.known_good_handle, args.private_root, args.checkout)
    except RuntimeFailure as failure:
        status = failure.status if failure.status in {"not_ready", "decision_required", "rejected", "unknown", "invalid", "unsupported"} else "rejected"
        _emit(result("host verify-backup", status=status))
        return exit_code(result("host verify-backup", status=status))
    except (OSError, ValueError):
        _emit(result("host verify-backup", status="rejected"))
        return 4
    except Exception:
        _emit(result("host verify-backup", status="rejected"))
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
