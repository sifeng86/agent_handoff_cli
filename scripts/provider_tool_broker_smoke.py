"""Offline broker integration with real agentctl/Git and synthetic missing auth."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.provider_auth_smoke import (
    APPROVED_KEY, _final_state, _make_wrapper, _read_json, _setup_fixture,
    _write_private_json,
)
from scripts.provider_bootstrap_smoke import read_context, write_private
from scripts.provider_auth_fixture import PROJECT, CONFIG
from scripts.provider_tool_broker import _clean_env


def _call(identifier, name, arguments):
    return {"jsonrpc": "2.0", "id": identifier, "method": "tools/call",
            "params": {"name": name, "arguments": arguments,
                       "_meta": {"progressToken": identifier}}}


def _exchange(config, calls, directory, label):
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "offline-acceptance", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        *calls,
    ]
    payload = b"".join((json.dumps(message) + "\n").encode() for message in messages)
    process = subprocess.run(
        [sys.executable, "-I", "-S", "-B", str(ROOT / "scripts/provider_tool_broker.py"),
         "--config-file", str(config)], input=payload, cwd=directory,
        env=_clean_env(directory), capture_output=True, timeout=120,
        close_fds=True, start_new_session=True,
    )
    write_private(directory / (label + ".stdout.jsonl"), process.stdout)
    write_private(directory / (label + ".stderr"), process.stderr)
    if process.returncode or process.stderr:
        raise RuntimeError("Offline broker exchange failed; inspect private fixture logs.")
    responses = [json.loads(line) for line in process.stdout.splitlines()]
    if [item.get("id") for item in responses] != [1, *[item["id"] for item in calls]]:
        raise RuntimeError("Offline broker response identity changed.")
    return {item["id"]: item["result"] for item in responses}, process.stdout


def _engine_result(reply):
    if reply.get("isError"):
        raise RuntimeError("Offline broker engine call failed; inspect private fixture logs.")
    return json.loads(reply["content"][0]["text"])


def prepare_fixture(agentctl):
    root = Path(tempfile.mkdtemp(prefix="agent-handoff-broker-engine-"))
    root.chmod(0o700)
    paths = _setup_fixture(root, Path(agentctl).resolve(strict=True))
    state, wrappers = root / "broker-private", root / "wrappers"
    state.mkdir(mode=0o700)
    wrappers.mkdir(mode=0o700)
    fake_doppler = root / "synthetic-doppler"
    write_private(fake_doppler, (
        "#!" + sys.executable + "\nimport sys\n"
        "if '--version' in sys.argv:\n print('Doppler CLI 3.77.0'); raise SystemExit(0)\n"
        "if 'get' in sys.argv:\n"
        " sys.stderr.write('{\"error\":\"you must provide a token\"}\\n'); raise SystemExit(1)\n"
        "raise SystemExit(0)\n"
    ).encode())
    fake_doppler.chmod(0o700)
    host = root / "synthetic-host-doppler"
    host.mkdir(mode=0o700)
    auth_config = root / "auth-proxy-config.json"
    config = {
        "real_cli": [str(Path(agentctl).resolve())],
        "authority": str(paths["authority"].destination), "ref": paths["authority"].publication_ref,
        "private_root": str(paths["actor_private"]), "context_file": str(paths["context_file"]),
        "source": str(paths["source"]), "managed": None, "resume_seen": False,
        "manifest_sha256": paths["manifest_sha256"], "destination_identity": paths["destination_identity"],
        "approved_key": APPROVED_KEY, "real_doppler": str(fake_doppler),
        "host_doppler_config": str(host), "doppler_root": str(root / "doppler-calls"),
        "wrapper_dir": str(wrappers), "engine_marker": "e" * 64,
        "agent_log": str(root / "agent-calls.jsonl"), "doppler_log": str(root / "doppler-calls.jsonl"),
        "verify_store": str(root / "proxy-verify.git"), "require_auth_failure_reason": True,
    }
    Path(config["doppler_root"]).mkdir(mode=0o700)
    for key in ("agent_log", "doppler_log"):
        write_private(Path(config[key]), b"")
    _write_private_json(auth_config, config)
    # Engine and nested Doppler proxies must not acquire the same config lock.
    doppler_config = root / "doppler-proxy-config.json"
    _write_private_json(doppler_config, config)
    _make_wrapper(wrappers / "doppler", "doppler_proxy_main", doppler_config)
    engine = state / "engine-proxy.py"
    _make_wrapper(engine, "agentctl_proxy_main", auth_config)
    policy = state / "policy.json"
    source_read_paths = ["AGENTS.md", "CLAUDE.md", ".agent/instructions.md",
                         ".agent/secrets.example.yaml"]
    _write_private_json(policy, {"version": 1, "cwd": str(paths["source"]),
        "read_files": [str(paths["source"] / name) for name in source_read_paths], "list_dirs": [],
        "protected_paths": [str(paths["context_file"]), str(auth_config)]})
    broker_config = state / "broker.json"
    _write_private_json(broker_config, {"version": 2, "policy_file": str(policy),
        "engine_proxy": str(engine), "cwd": str(paths["source"]),
        "managed_root": str(paths["actor_private"] / "repos"),
        "source_read_paths": source_read_paths})
    return root, paths, auth_config, broker_config, config


def run(agentctl):
    root, paths, auth_config, broker_config, config = prepare_fixture(agentctl)
    canonical = paths["source"] / "AGENTS.md"
    common = ["--authority", config["authority"], "--publication-ref", config["ref"],
        "--checkout", config["source"], "--private-root", config["private_root"],
        "--context-file", config["context_file"]]
    first, trace = _exchange(broker_config, [
        _call(2, "read_file", {"path": str(canonical)}),
        _call(3, "read_file", {"path": config["context_file"]}),
        _call(4, "agentctl", {"argv": ["inspect", *common]}),
        _call(5, "agentctl", {"argv": ["status", *common]}),
        _call(6, "agentctl", {"argv": ["resume", *common]}),
    ], root, "discovery")
    if first[2].get("isError") or not first[3].get("isError"):
        raise RuntimeError("Offline broker reader boundary failed.")
    for identifier in (4, 5):
        if _engine_result(first[identifier])["status"] != "completed":
            raise RuntimeError("Offline broker readonly discovery failed.")
    resumed = _engine_result(first[6])
    if resumed["status"] != "ready" or resumed["ownership"] != "owned":
        raise RuntimeError("Offline broker did not establish exact ownership.")
    managed = Path(resumed["managed_checkout"])
    managed_common = list(common)
    managed_common[managed_common.index("--checkout") + 1] = str(managed)
    verified, second_trace = _exchange(broker_config, [
        _call(3, "read_file", {"path": str(managed / "AGENTS.md")}),
        _call(4, "read_file", {"path": config["context_file"]}),
        _call(5, "read_file", {"path": str(managed / ".env")}),
        _call(6, "read_file", {"path": str(managed / "README.md")}),
        _call(2, "agentctl", {"argv": [
        "secrets", "verify", *managed_common, "--secrets-mode", "doppler",
        "--manifest-file", str(managed / ".agent/secrets.example.yaml"),
        "--doppler-project", PROJECT, "--doppler-config", CONFIG,
    ]})], root, "missing-auth")
    if (verified[3].get("isError") or
            verified[3]["content"][0]["text"] != canonical.read_text() or
            any(not verified[item].get("isError") for item in (4, 5, 6))):
        raise RuntimeError("Offline broker persisted managed grant boundary failed.")
    diagnostic = _engine_result(verified[2])
    if (diagnostic.get("shared_read_failure") != "authentication_required" or
            diagnostic.get("shared_read") != "unavailable" or diagnostic.get("status") != "not_ready"):
        raise RuntimeError("Offline broker missing-auth diagnostic changed.")
    context = read_context(paths["actor_private"], paths["source"])
    if context.encode() in trace + second_trace:
        raise RuntimeError("Offline broker disclosed private context.")
    _final_state(paths, _read_json(auth_config), context)
    evidence = {"schema_version": 1, "passed": True, "real_agentctl_engine": True,
        "real_isolated_git_authority": True, "canonical_read_completed": True,
        "private_context_read_denied": True, "private_context_absent_from_mcp_output": True,
        "readonly_inspect_status_and_owned_resume": True,
        "managed_public_read_after_broker_restart": True,
        "managed_unapproved_and_env_reads_denied": True,
        "optional_tool_metadata_dispatch_verified": True,
        "authentication_required_diagnostic": True, "doppler_cli_is_synthetic": True,
        "source_index_main_and_exact_owner_verified": True,
        "model_turn_started": False, "A35_complete": False, "A54_complete": False}
    _write_private_json(root / "evidence.json", evidence)
    return evidence, root


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--agentctl", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    evidence, _ = run(args.agentctl)
    if args.report:
        args.report.write_text(json.dumps(evidence, indent=2) + "\n")
    print(json.dumps(evidence))


if __name__ == "__main__":
    main()
