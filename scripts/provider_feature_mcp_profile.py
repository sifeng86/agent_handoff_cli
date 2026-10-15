"""Exclusive MCP launch profile and native trace adapter for feature-file tools."""
import hashlib
import json
import os
import re
import shlex
import stat
from pathlib import Path

from agent_handoff.public_results import validate_public_result
from scripts import provider_mcp_profile as common
from scripts import provider_tool_broker as transport
from scripts.provider_bootstrap_smoke import EvidenceError
from scripts.provider_project_files import MAX_FILE_SIZE, ProjectFileError, _validated_relative_path
from scripts.provider_readiness_smoke import EXIT_CODES
from scripts.provider_feature_broker import FeatureBroker


MODEL = common.MODEL
SERVER = common.SERVER
TOOL_NAMES = frozenset({"mcp__handoff__read_file", "mcp__handoff__agentctl", "mcp__handoff__project_file"})
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_OPERATION_ID = re.compile(r"[0-9a-f]{32}\Z")


def _feature_wrapper(path):
    try:
        candidate = Path(path)
        if (not candidate.is_absolute() or candidate.resolve(strict=True) != candidate or
                candidate.stat().st_size > transport.MAX_FRAME):
            raise ValueError("path")
        info = candidate.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
            raise ValueError("permissions")
        feature = FeatureBroker(candidate)
        return candidate, {"cwd": feature.config["cwd"]}
    except Exception:
        raise EvidenceError("feature_broker_config_invalid") from None


def write_feature_mcp_config(destination, feature_config):
    broker, loaded = _feature_wrapper(feature_config)
    return common._write_profile_config(destination, broker, loaded, "provider_feature_broker.py")


def _validate_feature_mcp_config(mcp_config):
    return common._validate_profile_config(
        mcp_config, broker_validator=_feature_wrapper,
        profile_builder=lambda cwd, broker: common._mcp_value(cwd, broker, "provider_feature_broker.py"))


def claude_feature_command(claude, mcp_config, model=MODEL):
    return common._claude_profile_command(
        claude, mcp_config, model, _validate_feature_mcp_config,
        "mcp__handoff__read_file,mcp__handoff__agentctl,mcp__handoff__project_file")


def _feature_error(code):
    raise EvidenceError(code)


def _read_arguments(arguments):
    if not isinstance(arguments, dict) or set(arguments) != {"path"} or not isinstance(arguments["path"], str):
        _feature_error("mcp_read_arguments_invalid")
    return common._read_event_path(arguments["path"])


def _agentctl_arguments(arguments):
    if not isinstance(arguments, dict) or set(arguments) != {"argv"}:
        _feature_error("mcp_feature_agent_arguments_invalid")
    argv = arguments["argv"]
    allowed = {("inspect",), ("status",), ("resume",), ("secrets", "inspect"), ("secrets", "verify")}
    if (not isinstance(argv, list) or any(not isinstance(item, str) for item in argv) or tuple(argv) not in allowed):
        _feature_error("mcp_feature_agent_arguments_invalid")
    return argv, " ".join(argv)


def _relative_feature_path(value):
    if not isinstance(value, str):
        _feature_error("mcp_project_arguments_invalid")
    try:
        return _validated_relative_path(value)
    except ProjectFileError:
        _feature_error("mcp_project_path_invalid")


def _project_arguments(arguments, *, allow_denied=False):
    if not isinstance(arguments, dict):
        _feature_error("mcp_project_arguments_invalid")
    action = arguments.get("action")
    if action == "read" and set(arguments) == {"action", "path"}:
        path = arguments["path"] if allow_denied else _relative_feature_path(arguments["path"])
        if not isinstance(path, str):
            _feature_error("mcp_project_arguments_invalid")
        return action, path, None
    if action == "write" and set(arguments) == {"action", "path", "content", "expected_sha256"}:
        path = arguments["path"] if allow_denied else _relative_feature_path(arguments["path"])
        content, expected = arguments["content"], arguments["expected_sha256"]
        if (not isinstance(path, str) or not isinstance(content, str) or not isinstance(expected, str) or
                (not allow_denied and not _DIGEST.fullmatch(expected))):
            _feature_error("mcp_project_arguments_invalid")
        if allow_denied:
            return action, path, {"content": content, "expected_sha256": expected}
        try:
            content_bytes = content.encode("utf-8", "strict")
        except UnicodeError:
            _feature_error("mcp_project_arguments_invalid")
        if len(content_bytes) > MAX_FILE_SIZE:
            _feature_error("mcp_project_arguments_invalid")
        return action, path, {"content": content, "expected_sha256": expected,
                              "desired_sha256": hashlib.sha256(content_bytes).hexdigest()}
    if action == "resolve" and set(arguments) == {"action", "operation_id"}:
        operation_id = arguments["operation_id"]
        if not isinstance(operation_id, str) or (not allow_denied and not _OPERATION_ID.fullmatch(operation_id)):
            _feature_error("mcp_project_arguments_invalid")
        return action, operation_id, None
    _feature_error("mcp_project_arguments_invalid")


def _project_result(action, identity, metadata, text):
    try:
        value = transport._json(text.encode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("shape")
        if action == "read":
            expected = {"path", "content", "sha256"}
            if (set(value) != expected or value["path"] != identity or not isinstance(value["content"], str) or
                    not isinstance(value["sha256"], str) or not _DIGEST.fullmatch(value["sha256"])):
                raise ValueError("read result")
            raw = value["content"].encode("utf-8", "strict")
            if len(raw) > MAX_FILE_SIZE or hashlib.sha256(raw).hexdigest() != value["sha256"]:
                raise ValueError("read digest")
        elif action == "write":
            expected = {"status", "path", "sha256", "operation_id"}
            if (set(value) != expected or value["status"] not in {"completed", "unknown"} or
                    value["path"] != identity or not isinstance(value["sha256"], str) or
                    not _DIGEST.fullmatch(value["sha256"]) or
                    value["sha256"] != metadata["desired_sha256"] or
                    not _OPERATION_ID.fullmatch(value["operation_id"])):
                raise ValueError("write result")
        else:
            expected = {"status", "path", "sha256", "operation_id"}
            if value.get("status") == "completed":
                expected.add("outcome")
                if value.get("outcome") not in {"applied", "not_applied"}:
                    raise ValueError("resolve outcome")
            elif value.get("status") != "unknown" or "outcome" in value:
                raise ValueError("resolve status")
            if (set(value) != expected or not isinstance(value["path"], str) or
                    _relative_feature_path(value["path"]) != value["path"] or
                    value["operation_id"] != identity or not _DIGEST.fullmatch(value["sha256"])):
                raise ValueError("resolve result")
        return value
    except EvidenceError:
        raise
    except Exception:
        _feature_error("mcp_project_result_invalid")


def _normalize_feature_item(name, arguments, block, identity, use_event, result_event):
    call_id = identity
    project_value = None
    failed = block.get("is_error", False)
    if type(failed) is not bool:
        _feature_error("mcp_tool_output_invalid")
    text = common._content_text(block.get("content", ""))
    if name == "mcp__handoff__read_file":
        path = _read_arguments(arguments)
        if not failed:
            if not path.is_absolute():
                _feature_error("mcp_read_path_invalid")
            common._canonical_path(str(path), "mcp_read_path_invalid")
        command = "cat " + shlex.quote(str(path))
    elif name == "mcp__handoff__agentctl":
        argv, _phase = _agentctl_arguments(arguments)
        command = shlex.join(["agentctl", *argv])
        if failed:
            return {"type": "item.completed", "item": {
                "type": "command_execution", "command": command, "exit_code": 1,
                "aggregated_output": text, "native_tool": name, "tool_arguments": arguments,
                "tool_call_id": call_id, "native_tool_use_event": use_event,
                "native_tool_result_event": result_event,
            }}
    elif name == "mcp__handoff__project_file":
        action, identity_value, metadata = _project_arguments(arguments, allow_denied=failed)
        command = shlex.join(["project_file", action, identity_value])
        if not failed:
            project_value = _project_result(action, identity_value, metadata, text)
    else:
        _feature_error("mcp_tool_unverified")
    if name == "mcp__handoff__agentctl" and not failed:
        argv, phase = _agentctl_arguments(arguments)
        try:
            value = validate_public_result(transport._json(text.encode("utf-8")))
        except Exception:
            _feature_error("mcp_agent_result_invalid")
        if value.get("command") != phase or value.get("status") not in EXIT_CODES:
            _feature_error("mcp_agent_result_invalid")
        exit_code = EXIT_CODES[value["status"]]
    elif name == "mcp__handoff__read_file":
        exit_code = 1 if failed else 0
    elif name == "mcp__handoff__project_file":
        exit_code = (1 if failed else 0 if action == "read" else
                     5 if project_value["status"] == "unknown" else 0)
    else:
        _feature_error("mcp_tool_unverified")
    return {"type": "item.completed", "item": {
        "type": "command_execution", "command": command,
        "exit_code": exit_code, "aggregated_output": text,
        "native_tool": name, "tool_arguments": arguments, "tool_call_id": call_id,
        "native_tool_use_event": use_event, "native_tool_result_event": result_event,
    }}


def claude_feature_trace(events, model=MODEL):
    return common._trace_native(events, model, TOOL_NAMES, _normalize_feature_item)
