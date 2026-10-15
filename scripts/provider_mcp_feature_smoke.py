"""Single-turn, exclusive-MCP acceptance controller for a bounded feature edit."""
import argparse
import ast
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from agent_handoff.git_backend import GitBackend, git_environment
from agent_handoff.publication import Runtime
from scripts.provider_auth_fixture import snapshot_host_config
from scripts.provider_auth_smoke import _private_snapshot_value, _read_json, _write_private_json
from scripts.provider_auth_smoke import _read_auth_doppler_calls
from scripts.provider_bootstrap_smoke import EvidenceError, provider_settings_snapshot, read_context, write_private
from scripts.provider_mcp_auth_smoke import (
    EVIDENCE_POINTER as AUTH_POINTER, TurnFailure, capture_turn, scan_native_output,
    scan_private_output,
)
from scripts.provider_feature_mcp_profile import (
    MODEL, claude_feature_command, claude_feature_trace, write_feature_mcp_config,
)
from scripts.provider_tool_broker_smoke import prepare_fixture
from scripts.provider_project_files import EDIT_PREFIX, _validate_edit_record

MAX_OUTPUT = 4 * 1024 * 1024
EVIDENCE_POINTER = Path("/tmp/agent-handoff-provider-mcp-feature-evidence.pointer")
_SOURCE_APP = b"def answer():\n    return 41\n"


def _safe_config(path):
    return _read_json(path)


def _make_feature_config(path, broker_config, config, paths):
    value = {
        "version": 1, "broker_config": str(Path(broker_config).resolve()),
        "authority": config["authority"], "publication_ref": config["ref"],
        "private_root": config["private_root"], "context_file": config["context_file"],
        "approved_paths": ["app.py"],
    }
    _write_private_json(path, value)
    return value


def _prompt(config):
    return ("Follow the repository instructions and complete this feature: change answer() in app.py to return 42. "
            "Use the approved feature tools for project files and runtime ownership. Do not publish or edit the original checkout.\n\n"
            f"Approved authority: {config['authority']}\nPublication ref: {config['ref']}\n"
            f"Original checkout: {config['source']}\nPrivate root: {config['private_root']}\n"
            f"Existing context file path: {config['context_file']}\n"
            "Feature tool syntax: agentctl argv is one of [\"inspect\"], [\"status\"], [\"resume\"], "
            "[\"secrets\",\"inspect\"], [\"secrets\",\"verify\"]. project_file accepts "
            "{action:\"read\",path:\"app.py\"} or {action:\"write\",path:\"app.py\",content:<text>,expected_sha256:<digest>}.\n").encode()


def _tool_items(normalized, events):
    """Native tool items with the raw event indices of their use and result."""
    order = {id(event): index for index, event in enumerate(events)}
    result = []
    for event in normalized:
        if event.get("type") != "item.completed" or not isinstance(event.get("item"), dict):
            continue
        item = event["item"]
        if item.get("type") == "command_execution" and item.get("native_tool"):
            try:
                use, done = order[id(item["native_tool_use_event"])], order[id(item["native_tool_result_event"])]
            except KeyError:
                raise EvidenceError("feature_trace_uncorrelated") from None
            if use >= done:
                raise EvidenceError("feature_trace_uncorrelated")
            result.append((use, done, item))
    return result


def _native_json(item):
    try:
        value = json.loads(item["aggregated_output"])
    except Exception:
        raise EvidenceError("feature_tool_result_invalid") from None
    if not isinstance(value, dict):
        raise EvidenceError("feature_tool_result_invalid")
    return value


def _app_is_answer_42(raw):
    try:
        tree = ast.parse(raw.decode("utf-8", "strict"))
    except Exception:
        return False
    if len(tree.body) != 1 or not isinstance(tree.body[0], ast.FunctionDef):
        return False
    function = tree.body[0]
    return (function.name == "answer" and not function.args.posonlyargs and not function.args.args and
            not function.args.defaults and not function.args.kw_defaults and function.returns is None and
            not function.type_params and not function.args.vararg and
            not function.args.kwarg and not function.args.kwonlyargs and not function.decorator_list and
            len(function.body) == 1 and isinstance(function.body[0], ast.Return) and
            isinstance(function.body[0].value, ast.Constant) and function.body[0].value.value == 42 and
            type(function.body[0].value.value) is int)


def _feature_journal(private_root, managed, authority, context_token, write):
    """Check the durable edit record of one write against its native receipt."""
    opid = write["value"].get("operation_id")
    if not isinstance(opid, str) or len(opid) != 32:
        raise EvidenceError("feature_journal_unproven")
    with Runtime(managed, private_root, authority, context_token) as runtime:
        filename = EDIT_PREFIX + opid + ".json"
        if filename not in os.listdir(runtime.store.fd):
            raise EvidenceError("feature_journal_unproven")
        record = _validate_edit_record(runtime.store.read(filename), opid, filename)
        oid, records = runtime._observe()
        session = runtime.sessions.prove(runtime.backend, oid, records)
        if (record["authority_identity"] != authority.identity or
                record["publication_ref"] != authority.publication_ref or
                record["session_id"] != session["session_id"] or
                record["checkout_identity"] != runtime.sessions.checkout_id):
            raise EvidenceError("feature_journal_unproven")
    desired = base64.b64decode(record["desired_b64"], validate=True)
    content = write["args"]["content"].encode("utf-8", "strict")
    if (record["phase"] != "resolved" or record["outcome"] != write["outcome"] or record["path"] != "app.py" or
            record["managed_checkout"] != str(managed) or desired != content or
            record["desired_sha256"] != hashlib.sha256(content).hexdigest() or
            record["desired_sha256"] != write["value"].get("sha256") or
            record["before_sha256"] != write["args"]["expected_sha256"]):
        raise EvidenceError("feature_journal_unproven")
    return record


def _evaluate(raw, stderr, token, config):
    # Scan the untouched provider bytes and stderr before native normalization.
    events = scan_native_output(raw, token)
    scan_private_output(stderr, token)
    normalized = claude_feature_trace(events, MODEL)
    items = _tool_items(normalized, events)
    calls = _read_auth_doppler_calls(config["agent_log"])
    if any(entry.get("origin") == "model" for entry in _read_auth_doppler_calls(config["doppler_log"])):
        raise EvidenceError("direct_model_doppler_call")

    # Engine log entries carry no origin: the broker is the only caller, so every entry must be
    # explained by exactly one successful native agentctl call, in order. Broker-denied native
    # calls (is_error) never reach the engine and are ignored; engine-denied or help entries are not.
    if any(entry.get("status") in ("denied", "help") or entry.get("phase") in ("help", "unknown")
           for entry in calls):
        raise EvidenceError("feature_engine_unexpected_phase")
    agent = []
    for use, done, item in items:
        # No engine status maps to exit 1, so exit 1 marks a broker-level rejection.
        if item["native_tool"] == "mcp__handoff__agentctl" and item["exit_code"] != 1:
            agent.append((use, done, " ".join(item["tool_arguments"]["argv"]), _native_json(item), item))
    if len(calls) != len(agent):
        raise EvidenceError("feature_engine_log_mismatch")
    for (_use, _done, phase, public, item), entry in zip(agent, calls):
        if (entry.get("phase") != phase or entry.get("status") != public.get("status") or
                entry.get("exit_code") != item["exit_code"]):
            raise EvidenceError("feature_engine_log_mismatch")
        if public.get("status") == "ready" and (
                public.get("ownership") != "owned" or entry.get("ownership") != "owned" or
                public.get("managed_checkout") != entry.get("managed_checkout")):
            raise EvidenceError("feature_resume_unproven")

    reads, project_reads, writes, resolves = [], [], [], []
    for use, done, item in items:
        name, args, code = item["native_tool"], item["tool_arguments"], item["exit_code"]
        if name == "mcp__handoff__read_file" and code == 0:
            reads.append((use, done, args["path"], item["aggregated_output"]))
        elif name == "mcp__handoff__project_file" and code in (0, 5):
            value = _native_json(item)
            if args["action"] == "read":
                project_reads.append({"use": use, "done": done, "path": args["path"], "value": value})
            elif args["action"] == "write":
                writes.append({"use": use, "done": done, "path": args["path"], "args": args, "value": value})
            else:
                resolves.append({"use": use, "done": done, "args": args, "value": value})

    canonical_path = str(Path(config["source"]) / ".agent/instructions.md")
    canonical_bytes = Path(canonical_path).read_text(encoding="utf-8")
    canonical = [done for _use, done, path, content in reads
                 if path == canonical_path and content == canonical_bytes]
    inspections = [(use, done) for use, done, phase, _v, _i in agent if phase in ("inspect", "status")]
    resumes = [(use, done, value) for use, done, phase, value, _i in agent if phase == "resume"]
    if not canonical or not inspections or min(canonical) >= min(use for use, _done in inspections):
        raise EvidenceError("canonical_discovery_order_unproven")
    owned = [(use, done, value) for use, done, value in resumes
             if value.get("status") == "ready" and value.get("ownership") == "owned"]
    if len(resumes) != 1 or len(owned) != 1:
        raise EvidenceError("feature_owned_resume_unproven")
    resume_use, ready_done, ready = owned[0]
    if not any(done < resume_use for _use, done in inspections):
        raise EvidenceError("feature_discovery_order_unproven")
    if any(entry["use"] < ready_done for entry in project_reads + writes + resolves):
        raise EvidenceError("feature_tool_before_ready")
    if not project_reads or not writes:
        raise EvidenceError("feature_read_write_unproven")

    for write in writes:
        reads_before = [entry for entry in project_reads if entry["path"] == "app.py" and entry["done"] < write["use"]]
        digest = reads_before[-1]["value"].get("sha256") if reads_before else None
        if write["path"] != "app.py" or digest is None or write["args"].get("expected_sha256") != digest:
            raise EvidenceError("feature_expected_digest_unproven")
        status = write["value"].get("status")
        if status == "completed":
            write["outcome"] = "applied"
        elif status == "unknown":
            matching = [entry for entry in resolves if entry["use"] > write["done"] and
                        entry["args"].get("operation_id") == write["value"].get("operation_id")]
            if not matching or matching[-1]["value"].get("status") != "completed" or \
                    matching[-1]["value"].get("outcome") not in ("applied", "not_applied"):
                raise EvidenceError("feature_write_resolution_unproven")
            write["outcome"] = matching[-1]["value"]["outcome"]
        else:
            raise EvidenceError("feature_write_resolution_unproven")
    applied = [write for write in writes if write["outcome"] == "applied"]
    if not applied:
        raise EvidenceError("feature_read_write_unproven")
    if not isinstance(config.get("managed"), str) or not config.get("resume_seen") or \
            ready.get("managed_checkout") != config["managed"]:
        raise EvidenceError("feature_resume_state_incoherent")
    return events, normalized, {"resume_count": len(resumes), "project_read_count": len(project_reads),
                                "project_write_count": len(writes), "managed": config["managed"],
                                "writes": writes, "applied": applied[-1]}


def _git(*args):
    return subprocess.run(["git", *args], capture_output=True, env=git_environment(), check=True).stdout


def _source_guard(paths):
    source = paths["source"]
    if ((source / "app.py").read_bytes() != _SOURCE_APP or
            _git("-C", str(source), "rev-parse", "HEAD").decode().strip() != paths["source_head"] or
            hashlib.sha256((source / ".git/index").read_bytes()).hexdigest() != paths["source_index"] or
            _git("-C", str(source), "status", "--porcelain=v1", "-z", "--untracked-files=all") or
            (source / ".env").exists()):
        raise EvidenceError("feature_source_changed")


def _mainline_guard(paths):
    if _git("--git-dir", str(paths["remote"]), "rev-parse", "refs/heads/main").decode().strip() != paths["base"]:
        raise EvidenceError("feature_mainline_changed")


def _publication_and_owner_guard(paths, config, token):
    """Publication stays at the init-derived revision; any owner is provable by the actor."""
    authority, remote = paths["authority"], paths["remote"]
    backend = GitBackend(Path(paths["root"]) / "verify-feature-final.git", str(remote), authority.publication_ref)
    revision = backend.observe()
    if not config.get("resume_seen"):
        if revision != paths["init_revision"] or backend.records(revision)["owner"]["owner"] is not None:
            raise EvidenceError("feature_publication_changed")
        return False
    if (revision != config.get("resume_revision") or
            _git("--git-dir", str(remote), "rev-parse", revision + "^").decode().strip() != paths["init_revision"]):
        raise EvidenceError("feature_publication_changed")
    managed = Path(config["managed"])
    with Runtime(managed, paths["actor_private"], authority, token) as runtime:
        oid, records = runtime._observe()
        session = runtime.sessions.prove(runtime.backend, oid, records)
        owner = records["owner"]["owner"]
        binding = runtime.store.read("checkout-binding.json")
        if (oid != revision or owner is None or owner.get("session_id") != session.get("session_id") or
                binding.get("checkout_identity") != session.get("checkout_identity")):
            raise EvidenceError("feature_owner_capability_unproven")
    return True


def _managed_guard(config):
    managed = Path(config["managed"])
    if managed.resolve(strict=True) != managed or not managed.is_relative_to(Path(config["private_root"]) / "repos"):
        raise EvidenceError("feature_managed_checkout_invalid")
    status = _git("-C", str(managed), "status", "--porcelain=v1", "-z", "--untracked-files=all")
    if status != b" M app.py\0":
        raise EvidenceError("feature_managed_tree_invalid")
    target = managed / "app.py"
    if (managed / ".env").exists() or target.is_symlink() or not _app_is_answer_42(target.read_bytes()):
        raise EvidenceError("feature_target_invalid")


def _final_feature_state(paths, config, token):
    _source_guard(paths)
    _mainline_guard(paths)
    owner = _publication_and_owner_guard(paths, config, token)
    if not owner:
        raise EvidenceError("feature_owner_capability_unproven")
    _managed_guard(config)
    return {"source_unchanged": True, "source_index_unchanged": True,
            "mainline_unchanged": True, "no_feature_publication": True,
            "owner_capability_proved": True, "managed_checkout_clean_except_app": True,
            "managed_app_answer_42": True}


def _preservation(paths, config, token):
    """Independent guards: one failing never hides the result of another."""
    checks = {"source_unchanged": False, "source_index_unchanged": False, "mainline_unchanged": False,
              "owner_capability_proved": False, "no_feature_publication": False}
    for keys, guard in ((("source_unchanged", "source_index_unchanged"), lambda: _source_guard(paths)),
                        (("mainline_unchanged",), lambda: _mainline_guard(paths))):
        try:
            guard()
            for key in keys:
                checks[key] = True
        except Exception:
            pass
    try:
        owner = _publication_and_owner_guard(paths, config, token)
        checks["no_feature_publication"] = True
        checks["owner_capability_proved"] = owner
    except Exception:
        pass
    return checks


def _private_prompt(paths, config):
    return _prompt(config)


def _persist(root, evidence):
    write_private(root / "feature-result.json", (json.dumps(evidence, sort_keys=True) + "\n").encode())
    write_private(EVIDENCE_POINTER, (str(root) + "\n").encode())


def run(agentctl, claude, model=MODEL, *, run_model=False):
    if not run_model:
        raise EvidenceError("model_execution_requires_explicit_opt_in")
    if model != MODEL:
        raise EvidenceError("claude_model_not_approved")
    agentctl, claude = Path(agentctl).resolve(strict=True), Path(claude).resolve(strict=True)
    if not os.access(agentctl, os.X_OK) or not os.access(claude, os.X_OK):
        raise EvidenceError("approved_executable_unavailable")
    root, paths, auth_config, broker_config, config = prepare_fixture(agentctl)
    root = Path(root)
    token = read_context(paths["actor_private"], paths["source"])
    feature_config = root / "broker-private" / "feature.json"
    _make_feature_config(feature_config, broker_config, config, paths)
    before_settings = provider_settings_snapshot("claude")
    before_host = snapshot_host_config()
    write_private(root / "provider-settings-baseline.json", (json.dumps(before_settings, sort_keys=True) + "\n").encode())
    write_private(root / "host-baseline.json", (json.dumps(_private_snapshot_value(before_host), sort_keys=True) + "\n").encode())
    write_private(root / "initial-git-baseline.json", (json.dumps({
        "source_head": paths["source_head"], "source_index": paths["source_index"],
        "base": paths["base"], "init_revision": paths["init_revision"]}, sort_keys=True) + "\n").encode())
    started = False
    raw = stderr = b""
    try:
        mcp = write_feature_mcp_config(root / "feature-mcp.json", feature_config)
        command = claude_feature_command(claude, mcp, model)
        try:
            _code, raw, stderr = capture_turn(command, _private_prompt(paths, config), paths["source"],
                                              env=git_environment())
            started = True
        except TurnFailure as failure:
            raw, stderr, started = failure.stdout, failure.stderr, failure.started
            raise
        write_private(root / "claude-turn.stdout.jsonl", raw)
        write_private(root / "claude-turn.stderr", stderr)
        if _code != 0:
            scan_native_output(raw, token)
            scan_private_output(stderr, token)
            raise EvidenceError("provider_process_failed")
        cfg = _safe_config(auth_config)
        _events, normalized, proof = _evaluate(raw, stderr, token, cfg)
        state = _final_feature_state(paths, cfg, token)
        if (Path(cfg["managed"]) / "app.py").read_bytes() != proof["applied"]["args"]["content"].encode("utf-8"):
            raise EvidenceError("feature_target_invalid")
        for write in proof["writes"]:
            _feature_journal(paths["actor_private"], Path(cfg["managed"]), paths["authority"], token, write)
        after_settings = provider_settings_snapshot("claude")
        after_host = snapshot_host_config()
        if before_settings != after_settings or before_host != after_host:
            raise EvidenceError("provider_or_host_configuration_changed")
        # Retain final prose only in private evidence for independent human review.
        from scripts.provider_interaction_smoke import final_assistant_text
        prose = final_assistant_text(_events)
        scan_private_output(prose.encode(), token)
        write_private(root / "assistant-prose.txt", prose.encode())
        evidence = {
            "schema_version": 1, "mechanical_scenario_passed": True,
            "agent_interaction_verified": False, "primary_prose_review_required": True,
            "provider": "claude", "model": MODEL, "model_started": True,
            "native_mcp_profile_verified": True, "feature_read_write_verified": True,
            "resume_count": proof["resume_count"], "owner_capability_proved": state["owner_capability_proved"],
            "source_unchanged": state["source_unchanged"], "source_index_unchanged": state["source_index_unchanged"],
            "mainline_unchanged": state["mainline_unchanged"], "no_feature_publication": state["no_feature_publication"],
            "managed_app_answer_42": state["managed_app_answer_42"],
            "managed_tree_clean_except_app": state["managed_checkout_clean_except_app"],
            "provider_settings_unchanged": True, "host_config_unchanged": True,
            "context_token_absent": True, "synthetic_doppler": True,
            "evidence_scope": "one fresh exclusive-MCP Claude Haiku 5.5 feature turn; isolated local Git and synthetic Doppler; app.py only",
        }
        _persist(root, evidence)
        return evidence
    except Exception as error:
        try:
            scan_private_output(raw, token)
            scan_private_output(stderr, token)
        except EvidenceError as scan_error:
            error = scan_error
        write_private(root / "claude-turn.stdout.jsonl", raw)
        write_private(root / "claude-turn.stderr", stderr)
        preservation = {"checked": False, "source_unchanged": False, "source_index_unchanged": False,
                        "mainline_unchanged": False, "owner_capability_proved": False,
                        "no_feature_publication": False, "host_config_unchanged": False,
                        "provider_settings_unchanged": False}
        try:
            cfg = _safe_config(auth_config)
            # Source, mainline and owner guards are independent of feature output validity.
            preservation.update(_preservation(paths, cfg, token))
            preservation["checked"] = True
        except Exception:
            pass
        for key, baseline, snapshot in (
                ("provider_settings_unchanged", "provider-settings-baseline.json",
                 lambda: provider_settings_snapshot("claude")),
                ("host_config_unchanged", "host-baseline.json",
                 lambda: _private_snapshot_value(snapshot_host_config()))):
            try:
                preservation[key] = _read_json(root / baseline) == snapshot()
            except Exception:
                pass
        code = str(error) if isinstance(error, EvidenceError) else "evidence_unverified"
        evidence = {"schema_version": 1, "mechanical_scenario_passed": False,
                    "agent_interaction_verified": False, "primary_prose_review_required": True,
                    "provider": "claude", "model": MODEL, "model_started": started,
                    "failure_category": code, "preservation_checks": preservation,
                    "private_evidence_pointer_written": True}
        _persist(root, evidence)
        return evidence


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--agentctl", type=Path, required=True)
    parser.add_argument("--claude", type=Path, required=True)
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--run-model", action="store_true")
    args = parser.parse_args(argv)
    report = run(args.agentctl, args.claude, args.model, run_model=args.run_model)
    print(json.dumps(report, sort_keys=True))
    return 0 if report.get("mechanical_scenario_passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
