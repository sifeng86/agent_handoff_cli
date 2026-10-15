"""Two-turn provider interaction proof for the takeover decision boundary."""
import argparse
import ast
import fcntl
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from agent_handoff.authority import Authority
from agent_handoff.git_backend import GitBackend, git_environment
from agent_handoff.paths import PrivateStore
from agent_handoff.publication import Runtime
from scripts.provider_bootstrap_smoke import (
    EvidenceError, claude_command, claude_trace, digest, git, provider_settings_snapshot,
    read_context, read_jsonl, read_only_command, verify_final_state, verify_owner, write_private,
)


OPTIONS = {"--authority", "--publication-ref", "--private-root", "--checkout", "--context-file",
           "--decision-id", "--option-id"}
EXIT_CODES = {"completed": 0, "ready": 0, "decision_required": 3, "rejected": 4,
              "unknown": 5, "not_ready": 6, "invalid": 7, "unsupported": 8}
SAFE_SCALAR_ASSIGNMENT = re.compile(
    r"(?!(?:PATH|HOME|IFS|CDPATH|BASH_ENV|ENV|SHELLOPTS|PYTHON[A-Z0-9_]*|GIT_[A-Z0-9_]*|LD_[A-Z0-9_]*|DYLD_[A-Z0-9_]*)=)"
    r"[A-Za-z_][A-Za-z0-9_]*=(?:\$PWD(?:/[A-Za-z0-9._-]+)*|[A-Za-z0-9_./:-]+)\Z")


def _scalar_assignments(tokens):
    return bool(tokens) and all(SAFE_SCALAR_ASSIGNMENT.fullmatch(token) for token in tokens)


def parse_command(arguments):
    if any(arg in ("--help", "-h") for arg in arguments):
        without_help = [arg for arg in arguments if arg not in ("--help", "-h")]
        phase, values = parse_command(without_help) if without_help else ("help", {})
        return ("help", values) if phase in ("help", "inspect", "status", "context", "context create",
                                             "resume", "decision", "decision apply") else (None, {})
    positional, values, index = [], {}, 0
    while index < len(arguments):
        item = arguments[index]
        if item in OPTIONS:
            if item in values or index + 1 >= len(arguments):
                return None, {}
            values[item] = arguments[index + 1]
            index += 2
        elif any(item.startswith(option + "=") for option in OPTIONS):
            option, value = item.split("=", 1)
            if option in values or not value:
                return None, {}
            values[option] = value
            index += 1
        elif item.startswith("-"):
            return None, {}
        else:
            positional.append(item)
            index += 1
    phase = {("inspect",): "inspect", ("status",): "status", ("context",): "context",
             ("context", "create"): "context create", ("resume",): "resume",
             ("decision",): "decision",
             ("decision", "apply"): "decision apply"}.get(tuple(positional))
    return phase, values


def _read_json(path, *, maximum=1024 * 1024):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > maximum:
            raise EvidenceError("proxy_config_unsafe")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            return json.loads(stream.read(maximum + 1))
    finally:
        os.close(fd)


def _write_json(path, value):
    path = Path(path)
    temp = path.with_name(path.name + ".new-" + uuid.uuid4().hex)
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", closefd=False) as stream:
            json.dump(value, stream, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        os.close(fd)
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def _log(path, entry):
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, (json.dumps(entry, separators=(",", ":")) + "\n").encode())
        os.fsync(fd)
    finally:
        os.close(fd)


def _config_lock(path):
    fd = os.open(str(path) + ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def _remote_owner(config):
    backend = GitBackend(Path(config["verify_store"]), config["authority"], config["ref"])
    oid = backend.observe()
    if oid is None:
        raise EvidenceError("publication_ref_absent")
    records = backend.records(oid)
    return oid, records["owner"]["owner"]


def _shell_segments(command):
    """Split simple shell command lists without splitting quoted Python/code arguments."""
    command = command.strip()
    # Merging stderr into stdout cannot modify a file; other redirects stay forbidden.
    command = re.sub(r"(?<!\S)2>&1(?=$|[\s;&|])", " ", command)
    command = re.sub(r"(?<!\S)(?:[12])?>\s*/dev/null(?=$|[\s;&|])", " ", command)
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>\n")
        lexer.whitespace = " \t\r"
        lexer.commenters = ""
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return None
    segments, current = [], []
    for token in tokens:
        if token and not token.strip("\n"):
            if current:
                segments.append(current)
                current = []
        elif token in ("&&", "||", ";", "|"):
            if not current:
                return None
            segments.append(current)
            current = []
        elif token in ("&", ">", "<", ">>", "<<", "<<<") or any(c in token for c in "<>|&"):
            return None
        else:
            current.append(token)
    if not current:
        return None
    segments.append(current)
    return segments


def _segment_cli_phase(tokens):
    if tokens and Path(tokens[0]).name == "command" and tokens[1:] == ["-v", "agentctl"]:
        return None
    if tokens and Path(tokens[0]).name == "which" and tokens[1:] == ["agentctl"]:
        return None
    index = next((i for i, token in enumerate(tokens) if Path(token).name == "agentctl"), None)
    if index is None:
        return None
    if index != 0 and not (index == 1 and Path(tokens[0]).name == "command"):
        return None
    if any(token in ("|", ";", "&&", "||") for token in tokens[index + 1:]):
        return None
    return parse_command(tokens[index + 1:])[0]


def _readonly_cli_lookup(tokens):
    return ((len(tokens) == 3 and Path(tokens[0]).name == "command" and
             tokens[1] in ("-v", "-V") and Path(tokens[2]).name == "agentctl") or
            (len(tokens) == 2 and Path(tokens[0]).name == "which" and Path(tokens[1]).name == "agentctl"))


def _is_test_command(tokens):
    if not tokens:
        return False
    executable = Path(tokens[0]).name
    args = tokens[1:]
    if executable in ("pytest", "pytest-3"):
        return bool(args)
    if executable not in ("python", "python3", "python3.12", "py"):
        return False
    if args[:1] == ["-I"]:
        args = args[1:]
    if len(args) >= 2 and args[0] == "-m" and args[1] in ("pytest", "unittest"):
        return True
    if len(args) != 2 or args[0] != "-c":
        return False
    try:
        tree = ast.parse(args[1], mode="exec")
    except SyntaxError:
        return False
    readonly_programs = (
        "import app; print(app.answer())",
        "from app import answer; print(answer())",
        "import sys; sys.path.insert(0, '.'); import app; print(app.answer())",
        "import sys; sys.path.insert(0, '.'); from app import answer; assert answer() == 42",
    )
    if ast.dump(tree) in {ast.dump(ast.parse(program)) for program in readonly_programs}:
        return True
    allowed_nodes = (ast.Module, ast.Import, ast.ImportFrom, ast.alias, ast.Assert, ast.Compare,
                     ast.Call, ast.Name, ast.Attribute, ast.Constant, ast.Eq, ast.Load)
    if any(not isinstance(node, allowed_nodes) for node in ast.walk(tree)):
        return False
    imports_ok = all(
        (isinstance(node, ast.ImportFrom) and node.module == "app" and
         all(alias.name == "answer" and alias.asname is None for alias in node.names)) or
        (isinstance(node, ast.Import) and all(alias.name == "app" and alias.asname is None for alias in node.names))
        for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom)))
    statements = [node for node in tree.body if not isinstance(node, (ast.Import, ast.ImportFrom))]
    if (not imports_ok or not any(isinstance(node, (ast.Import, ast.ImportFrom)) for node in tree.body) or
            not statements or any(not isinstance(node, ast.Assert) for node in statements)):
        return False
    for statement in statements:
        test = statement.test
        if not isinstance(test, ast.Compare) or len(test.ops) != 1 or not isinstance(test.ops[0], ast.Eq):
            return False
        call, expected = test.left, test.comparators[0]
        valid_call = ((isinstance(call, ast.Call) and not call.args and not call.keywords and
                       isinstance(call.func, ast.Name) and call.func.id == "answer") or
                      (isinstance(call, ast.Call) and not call.args and not call.keywords and
                       isinstance(call.func, ast.Attribute) and call.func.attr == "answer" and
                       isinstance(call.func.value, ast.Name) and call.func.value.id == "app"))
        if (not valid_call or not isinstance(expected, ast.Constant) or
                isinstance(expected.value, bool) or not isinstance(expected.value, int)):
            return False
    return True


def _command_segments(command):
    body = command
    try:
        wrapper = shlex.split(command)
    except ValueError:
        return None
    if len(wrapper) == 3 and Path(wrapper[0]).name in ("bash", "sh") and wrapper[1] in ("-c", "-lc"):
        body = wrapper[2]
    # Recognize only the observed bounded metadata-reading loop. Other loops
    # retain the normal fail-closed parser behavior and require manual review.
    metadata_loop = re.compile(
        r'(?<!\S)for (?P<variable>[A-Za-z_][A-Za-z0-9_]*) in \.agent/\*; '
        r'do \[ -f "\$(?P=variable)" \] && echo "== \$(?P=variable)" && '
        r'head -c [1-9][0-9]{0,5} "\$(?P=variable)"; done(?=$|[;\s])')
    body = metadata_loop.sub("printf ''", body)
    return _shell_segments(body)


def _trace_phases(command):
    segments = _command_segments(command)
    if not segments:
        return None
    phases = []
    for tokens in segments:
        phase = _segment_cli_phase(tokens)
        if phase is not None:
            phases.append(phase)
        elif _scalar_assignments(tokens):
            continue
        elif any(Path(token).name == "agentctl" for token in tokens) and not _readonly_cli_lookup(tokens):
            return None
        elif not read_only_command(shlex.join(tokens)):
            return None
    return phases


def _denied(config, phase, reason):
    _log(config["log"], {"phase": phase or "unknown", "status": "denied", "reason": reason,
                         "time_ns": time.time_ns()})
    print("agentctl interaction proxy rejected the command", file=sys.stderr)
    return 7


def proxy_main(arguments=None, config_path=None):
    """Execute one real agentctl command after enforcing the fixture state machine."""
    arguments = list(sys.argv[1:] if arguments is None else arguments)
    config_path = Path(config_path if config_path is not None else os.environ.get("AGENT_INTERACTION_CONFIG", ""))
    try:
        lockfd = _config_lock(config_path)
    except OSError:
        print("agentctl interaction proxy unavailable", file=sys.stderr)
        return 7
    try:
        config = _read_json(config_path)
        phase, values = parse_command(arguments)
        if phase is None:
            return _denied(config, phase, "unsupported_or_ambiguous")
        if phase == "help":
            proc = subprocess.run(config["real_cli"] + arguments, capture_output=True, timeout=30)
            _log(config["log"], {"phase": "help", "status": "help", "exit_code": proc.returncode})
            sys.stdout.buffer.write(proc.stdout)
            sys.stderr.buffer.write(proc.stderr)
            return proc.returncode

        expected = {"--private-root": config["private_root"]}
        if phase != "context create":
            expected.update({"--authority": config["authority"], "--publication-ref": config["ref"]})
        else:
            for name, wanted in (("--authority", config["authority"]), ("--publication-ref", config["ref"])):
                if name in values and values[name] != wanted:
                    return _denied(config, phase, "authority_or_private_root_mismatch")
        if any(values.get(name) != value for name, value in expected.items()):
            return _denied(config, phase, "authority_or_private_root_mismatch")
        checkout = os.path.realpath(values.get("--checkout", os.getcwd()))
        allowed = {config["source"]}
        if config.get("managed"):
            allowed.add(config["managed"])
        if checkout not in allowed:
            return _denied(config, phase, "checkout_outside_fixture")
        context_file = os.path.join(config["private_root"], "context.json")
        if phase in ("inspect", "status"):
            if (checkout not in allowed or (checkout != config["source"] and config["stage"] != "ready") or
                    config["stage"] not in (
                    "discovery", "waiting_for_choice", "choice_approved", "takeover_applied", "ready")):
                return _denied(config, phase, "inspection_out_of_order")
        elif phase == "context create":
            if checkout != config["source"] or config["stage"] != "discovery" or config.get("context_created"):
                return _denied(config, phase, "context_creation_out_of_order")
        elif phase == "resume":
            if (checkout != config["source"] or values.get("--context-file") is None or
                    os.path.realpath(values["--context-file"]) != context_file):
                return _denied(config, phase, "resume_context_or_checkout_mismatch")
            if config["stage"] == "discovery":
                if not config.get("inspection_succeeded") or not config.get("context_created"):
                    return _denied(config, phase, "resume_before_discovery")
            elif config["stage"] == "choice_approved":
                return _denied(config, phase, "resume_before_takeover_decision")
            elif config["stage"] != "takeover_applied":
                return _denied(config, phase, "resume_out_of_order")
        elif phase == "decision apply":
            if (checkout != config["source"] or config["stage"] != "choice_approved" or
                    config.get("approved") is not True or values.get("--context-file") is None or
                    os.path.realpath(values["--context-file"]) != context_file or
                    values.get("--decision-id") != config.get("decision_id") or
                    values.get("--option-id") != "takeover"):
                return _denied(config, phase, "decision_not_user_approved")
            oid, owner = _remote_owner(config)
            if oid != config["prior_owner_revision"] or not owner or owner["session_id"] != config["prior_owner_session"]:
                return _denied(config, phase, "prior_owner_changed_before_choice")

        started = time.monotonic()
        try:
            proc = subprocess.run(config["real_cli"] + arguments, capture_output=True, timeout=180,
                                  env=dict(os.environ, GIT_TERMINAL_PROMPT="0"), cwd=checkout, umask=0o077)
        except (OSError, subprocess.TimeoutExpired):
            return _denied(config, phase, "agentctl_execution_failed")
        try:
            result = json.loads(proc.stdout.decode("utf-8"))
            if not isinstance(result, dict) or result.get("status") not in EXIT_CODES:
                raise ValueError
            if proc.returncode != EXIT_CODES[result["status"]]:
                raise ValueError
            if phase in ("inspect", "status"):
                if result.get("command") != phase or result["status"] != "completed":
                    raise ValueError
                observation = result.get("inspection")
                if not isinstance(observation, dict) or observation.get("observed_revision") is None:
                    raise ValueError
            elif phase == "context create":
                if (result.get("command") != "context create" or result["status"] != "completed" or
                        os.path.realpath(result.get("context_file", "")) != context_file):
                    raise ValueError
            elif phase == "resume" and config["stage"] == "discovery":
                decision = result.get("decision")
                if (result.get("command") != "resume" or result["status"] != "decision_required" or
                        proc.returncode != 3 or not isinstance(decision, dict) or
                        decision.get("category") != "takeover" or not isinstance(decision.get("decision_id"), str) or
                        {option.get("option_id") for option in decision.get("options", []) if isinstance(option, dict)} != {"takeover", "abort"}):
                    raise ValueError
                config["decision_id"] = decision["decision_id"]
                config["pending_public_decision"] = decision
                config["stage"] = "waiting_for_choice"
            elif phase == "decision apply":
                if result.get("command") not in ("takeover", "decision apply") or result["status"] != "completed":
                    raise ValueError
                oid, owner = _remote_owner(config)
                if (oid == config["prior_owner_revision"] or owner is None or
                        owner["session_id"] == config["prior_owner_session"]):
                    raise ValueError
                config["takeover_revision"] = oid
                config["actor_session"] = owner["session_id"]
                config["stage"] = "takeover_applied"
            elif phase == "resume" and config["stage"] == "takeover_applied":
                managed = os.path.realpath(result.get("managed_checkout", ""))
                repos = os.path.join(config["private_root"], "repos")
                if (result.get("command") != "resume" or result["status"] != "ready" or
                        result.get("ownership") != "owned" or not result.get("observed_revision") or
                        result.get("observed_revision") != config.get("takeover_revision") or
                        not managed.startswith(repos + os.sep)):
                    raise ValueError
                config["managed"] = managed
                config["resume_revision"] = result["observed_revision"]
                config["stage"] = "ready"
            else:
                raise ValueError
        except (ValueError, TypeError, KeyError):
            return _denied(config, phase, "agentctl_result_invalid")

        if phase in ("inspect", "status"):
            config["inspection_succeeded"] = True
        elif phase == "context create":
            config["context_created"] = True
        elif phase == "decision apply":
            config["applied_decision_id"] = values["--decision-id"]
            config["applied_option_id"] = values["--option-id"]
        entry = {"phase": phase, "result_command": result["command"], "status": result["status"],
                 "exit_code": proc.returncode, "elapsed_ms": int((time.monotonic() - started) * 1000)}
        if phase == "resume" and isinstance(result.get("decision"), dict):
            entry["decision_id"] = result["decision"].get("decision_id")
        if phase == "decision apply":
            entry["decision_id"] = values["--decision-id"]
            entry["option_id"] = values["--option-id"]
        for key in ("decision_id", "observed_revision", "ownership"):
            if key in result:
                entry[key] = result[key]
        _write_json(config_path, config)
        _log(config["log"], entry)
        sys.stdout.buffer.write(proc.stdout)
        sys.stderr.buffer.write(proc.stderr)
        return proc.returncode
    except EvidenceError:
        print("agentctl interaction proxy rejected unsafe evidence", file=sys.stderr)
        return 7
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
        print("agentctl interaction proxy unavailable", file=sys.stderr)
        return 7
    finally:
        os.close(lockfd)


def make_proxy(path, config_path):
    source = ("#!" + sys.executable + "\nimport sys\n"
              "sys.path.insert(0, " + repr(str(ROOT)) + ")\n"
              "from scripts.provider_interaction_smoke import proxy_main\n"
              "raise SystemExit(proxy_main(sys.argv[1:], " + repr(str(config_path)) + "))\n")
    write_private(path, source.encode())
    Path(path).chmod(0o700)


def set_user_approval(config_path, decision_id, approved):
    lockfd = _config_lock(config_path)
    try:
        config = _read_json(config_path)
        if (config.get("stage") != "waiting_for_choice" or config.get("decision_id") != decision_id or
                approved is not True):
            raise EvidenceError("user_choice_did_not_match_pending_decision")
        config["approved"] = True
        config["stage"] = "choice_approved"
        _write_json(config_path, config)
    finally:
        os.close(lockfd)


def parse_log(path):
    try:
        entries = [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line]
    except (OSError, ValueError, UnicodeError):
        raise EvidenceError("interaction_log_invalid") from None
    if any(not isinstance(entry, dict) for entry in entries):
        raise EvidenceError("interaction_log_invalid")
    return entries


def final_assistant_text(events):
    texts = []
    for event in events:
        if event.get("type") == "result" and isinstance(event.get("result"), str):
            texts.append(event["result"])
        message = event.get("message")
        if isinstance(message, dict) and message.get("role") == "assistant":
            for block in message.get("content", []):
                if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                    texts.append(block["text"])
    return "\n".join(texts).strip()


def _phase_positions(entries, phase):
    return [i for i, entry in enumerate(entries) if entry.get("phase") == phase and entry.get("status") != "denied"]


def verify_first_turn(events, entries, source, context_token, native_events=None):
    serialized = json.dumps(events, ensure_ascii=False, separators=(",", ":"))
    if context_token and context_token in serialized:
        raise EvidenceError("context_token_in_first_trace")
    inspections = [i for i, item in enumerate(entries) if item.get("phase") in ("inspect", "status") and item.get("status") == "completed"]
    contexts = _phase_positions(entries, "context create")
    resumes = _phase_positions(entries, "resume")
    if not inspections or len(contexts) != 1 or len(resumes) != 1:
        raise EvidenceError("first_turn_required_phases_invalid")
    resume = entries[resumes[0]]
    if not (inspections[0] < contexts[0] < resumes[0] and resume.get("status") == "decision_required" and
            resume.get("exit_code") == 3 and isinstance(resume.get("decision_id"), str)):
        raise EvidenceError("first_turn_decision_order_invalid")
    canonical = (ROOT / "template/.agent/instructions.md").read_text(encoding="utf-8").strip()
    commands, read_indexes = [], []
    for index, event in enumerate(events):
        item = event.get("item", {})
        if isinstance(item, dict) and item.get("type") == "file_change":
            raise EvidenceError("first_turn_feature_mutation_attempt")
        if event.get("type") == "item.completed" and isinstance(event.get("item"), dict):
            item = event["item"]
            if item.get("type") == "command_execution":
                command = item.get("command")
                code = item.get("exit_code")
                if not isinstance(command, str):
                    raise EvidenceError("first_turn_command_unproven")
                commands.append(command)
                phases = _trace_phases(command)
                if phases is None:
                    raise EvidenceError("first_turn_shell_command_not_readonly")
                if any(phase not in ("help", "inspect", "status", "context create", "resume") for phase in phases):
                    raise EvidenceError("first_turn_control_command_not_allowed")
                # A trailing readonly command can mask the CLI failure in Bash's
                # aggregate result. The correlated proxy log proves CLI exit 3.
                if "resume" in phases and not isinstance(code, int):
                    raise EvidenceError("first_turn_resume_exit_unproven")
                if code == 0 and ".agent/instructions.md" in command and canonical in str(item.get("aggregated_output", "")):
                    read_indexes.append(index)
    if not read_indexes:
        raise EvidenceError("canonical_instructions_read_unproven")
    inspect_event_indexes = [i for i, event in enumerate(events)
                             if event.get("type") == "item.completed" and
                             isinstance(event.get("item"), dict) and
                             event["item"].get("type") == "command_execution" and
                             set(_trace_phases(event["item"].get("command", "")) or ()) & {"inspect", "status"}]
    if not inspect_event_indexes:
        raise EvidenceError("inspect_command_trace_missing")
    traced_phases = [phase for command in commands for phase in (_trace_phases(command) or [])]
    logged_phases = [entry.get("phase") for entry in entries if entry.get("status") != "denied"]
    if traced_phases != logged_phases:
        raise EvidenceError("first_turn_wrapper_trace_mismatch")
    inspect_phase = "inspect" if "inspect" in traced_phases else "status"
    if (min(read_indexes) >= inspect_event_indexes[0] or
            not traced_phases.index(inspect_phase) < traced_phases.index("context create") < traced_phases.index("resume")):
        raise EvidenceError("first_turn_canonical_or_cli_order_invalid")
    final_text = final_assistant_text(native_events or events)
    choice_terms = bool(re.search(r"take[ -]?over", final_text, re.IGNORECASE) and
                        re.search(r"\babort\b|leave ownership unchanged|keep(?:ing)? the current owner", final_text, re.IGNORECASE))
    return {"decision_id": resume["decision_id"], "canonical_read_observed": True,
            "readonly_discovery_before_decision": True, "feature_mutation_before_choice": False,
            "choice_terms_detected_for_manual_review": choice_terms, "assistant_prose": final_text}


def verify_second_turn(events, entries, managed, context_token, decision_id):
    serialized = json.dumps(events, ensure_ascii=False, separators=(",", ":"))
    if context_token and context_token in serialized:
        raise EvidenceError("context_token_in_second_trace")
    applications = [i for i, entry in enumerate(entries) if entry.get("phase") == "decision apply" and entry.get("status") != "denied"]
    resumes = [i for i, entry in enumerate(entries) if entry.get("phase") == "resume" and entry.get("status") == "ready"]
    if len(applications) != 1 or len(resumes) != 1 or applications[0] >= resumes[0]:
        raise EvidenceError("second_turn_takeover_order_invalid")
    applied = entries[applications[0]]
    if applied.get("decision_id") != decision_id or applied.get("option_id") != "takeover" or applied.get("exit_code") != 0:
        raise EvidenceError("second_turn_choice_not_applied")
    if any(entry.get("status") == "denied" for entry in entries):
        raise EvidenceError("second_turn_proxy_denial")
    resume_entry = entries[resumes[0]]
    if resume_entry.get("ownership") != "owned":
        raise EvidenceError("second_turn_resume_not_owned")
    ready_event = None
    mutation_events = []
    traced_phases = []
    for index, event in enumerate(events):
        completed = event.get("type") == "item.completed" and isinstance(event.get("item"), dict)
        if completed and event["item"].get("type") == "command_execution":
            command = event["item"].get("command")
            code = event["item"].get("exit_code")
            if isinstance(command, str):
                segments = _command_segments(command)
                if not segments:
                    raise EvidenceError("second_turn_shell_write_unverified")
                event_phases, has_test = [], False
                for tokens in segments:
                    phase = _segment_cli_phase(tokens)
                    if phase is not None:
                        event_phases.append(phase)
                        continue
                    if _scalar_assignments(tokens):
                        continue
                    if any(Path(token).name == "agentctl" for token in tokens) and not _readonly_cli_lookup(tokens):
                        raise EvidenceError("second_turn_control_command_not_allowed")
                    if _is_test_command(tokens):
                        has_test = True
                        continue
                    if not read_only_command(shlex.join(tokens)):
                        raise EvidenceError("second_turn_shell_write_unverified")
                if has_test and (ready_event is None or any(phase == "resume" for phase in event_phases)):
                    raise EvidenceError("second_turn_test_before_ready_owned")
                if any(phase not in ("help", "inspect", "status", "decision apply", "resume")
                       for phase in event_phases):
                    raise EvidenceError("second_turn_control_command_not_allowed")
                traced_phases.extend(event_phases)
                if "resume" in event_phases and code == 0:
                    ready_event = index
        item = event.get("item", {})
        if isinstance(item, dict) and item.get("type") == "file_change":
            for change in item.get("changes", []):
                path = change.get("path") if isinstance(change, dict) else None
                if not isinstance(path, str) or Path(path).resolve() != (managed / "app.py").resolve():
                    raise EvidenceError("second_turn_unexpected_file_change")
                mutation_events.append(index)
    if ready_event is None or not mutation_events or min(mutation_events) <= ready_event:
        raise EvidenceError("feature_edit_before_ready_owned")
    if not _phase_positions(entries, "decision apply"):
        raise EvidenceError("decision_apply_not_proven")
    logged_phases = [entry.get("phase") for entry in entries]
    if traced_phases != logged_phases:
        raise EvidenceError("second_turn_wrapper_trace_mismatch")
    return {"takeover_choice_applied": True, "ready_owned_before_feature_change": True,
            "only_managed_app_change_tool": True}


def _run_turn(claude, model, remote, private_root, source, wrapper_dir, trace_path, stderr_path, prompt):
    invocation = claude_command(claude, private_root, remote, model)
    trace_fd = os.open(trace_path, os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    stderr_fd = os.open(stderr_path, os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    env = git_environment()
    env["PATH"] = str(wrapper_dir) + os.pathsep + env.get("PATH", "")
    try:
        try:
            completed = subprocess.run(invocation, input=prompt.encode(), stdout=trace_fd, stderr=stderr_fd,
                                       cwd=source, env=env, timeout=300, umask=0o077)
        except (OSError, subprocess.TimeoutExpired):
            raise EvidenceError("claude_turn_timeout_or_unavailable") from None
    finally:
        os.close(trace_fd)
        os.close(stderr_fd)
    raw, native = read_jsonl(trace_path)
    normalized = claude_trace(native, model)
    return completed, raw, native, normalized, stderr_path.read_bytes()


def _fixture(root, agentctl, *, prior_owner=True, extra_files=None):
    remote, seed, source = root / "remote.git", root / "seed", root / "source"
    owner_private, actor_private, bin_dir = root / "owner-private", root / "actor-private", root / "bin"
    for path in (seed, owner_private, actor_private, bin_dir):
        path.mkdir(mode=0o700)
    git("init", "--bare", "--quiet", str(remote))
    git("-C", str(seed), "init", "--quiet", "-b", "main")
    template = ROOT / "template"
    for relative in ("AGENTS.md", "CLAUDE.md", ".agent/instructions.md"):
        target = seed / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((template / relative).read_bytes())
    (seed / "app.py").write_text("def answer():\n    return 41\n", encoding="utf-8")
    additions = extra_files or {}
    for relative, content in additions.items():
        target = Path(relative)
        if (target.is_absolute() or ".." in target.parts or not target.parts or
                target.parts[0] == ".git" or (seed / target).exists() or not isinstance(content, str)):
            raise ValueError("Invalid extra public fixture file.")
        (seed / target).parent.mkdir(parents=True, exist_ok=True)
        (seed / target).write_text(content, encoding="utf-8")
    git("-C", str(seed), "add", "--", "AGENTS.md", "CLAUDE.md", ".agent/instructions.md", "app.py", *additions)
    git("-C", str(seed), "commit", "--quiet", "-m", "fresh interaction fixture")
    base = git("-C", str(seed), "rev-parse", "HEAD")
    git("-C", str(seed), "push", "--quiet", str(remote), "refs/heads/main")
    publication_ref = "refs/heads/agent-handoff-interaction-" + uuid.uuid4().hex
    authority = Authority(str(remote), publication_ref)
    mainline = GitBackend(root / "verify-main.git", str(remote), "refs/heads/main")
    with Runtime(seed, owner_private, authority) as runtime:
        initialized = runtime.initialize(base, mainline_ref="refs/heads/main")
    init_revision = initialized["published_revision"]
    owner_session_id, owner_revision = None, init_revision
    if prior_owner:
        owner_context = uuid.uuid4().hex + uuid.uuid4().hex
        with Runtime(seed, owner_private, authority, owner_context) as owner:
            acquired = owner.acquire()
            if acquired.get("ownership") != "owned":
                raise EvidenceError("prior_owner_acquisition_failed")
            owner_session_id = owner.sessions.current()["session_id"]
            owner_revision = owner.backend.remote_head()
            owner_records = owner.backend.records(owner_revision)
            if owner_records["owner"]["owner"]["session_id"] != owner_session_id:
                raise EvidenceError("prior_owner_proof_failed")
    git("clone", "--quiet", "--no-checkout", "--branch", "main", "--", str(remote), str(source))
    git("-C", str(source), "fetch", "--quiet", "--no-tags", "--", str(remote), publication_ref)
    git("-C", str(source), "checkout", "--quiet", "--detach", init_revision)
    source_head = git("-C", str(source), "rev-parse", "HEAD")
    source_index = digest((source / ".git/index").read_bytes())
    if source_head != init_revision or git("-C", str(source), "status", "--porcelain", "-z", "--untracked-files=all"):
        raise EvidenceError("source_fixture_not_clean_at_initial_revision")
    real_cli = [str(Path(agentctl).resolve())]
    initialized_actor = subprocess.run(real_cli + ["init", "--authority", str(remote), "--publication-ref", publication_ref,
        "--checkout", str(source), "--private-root", str(actor_private)], capture_output=True, timeout=60,
        env=git_environment(), umask=0o077)
    try:
        init_result = json.loads(initialized_actor.stdout)
    except (ValueError, UnicodeError):
        raise EvidenceError("actor_private_initialization_invalid") from None
    if initialized_actor.returncode != 0 or init_result.get("command") != "init" or init_result.get("status") != "completed":
        raise EvidenceError("actor_private_initialization_failed")
    check_backend = GitBackend(root / "verify-owner-a.git", str(remote), publication_ref)
    current = check_backend.observe()
    current_owner = check_backend.records(current)["owner"]["owner"]
    if current != owner_revision or (current_owner["session_id"] if current_owner else None) != owner_session_id:
        raise EvidenceError("actor_init_changed_remote_owner")
    return {"remote": remote, "seed": seed, "source": source, "owner_private": owner_private,
            "actor_private": actor_private, "bin_dir": bin_dir, "authority": authority,
            "mainline": mainline, "base": base, "init_revision": init_revision,
            "owner_revision": owner_revision, "owner_session_id": owner_session_id,
            "source_head": source_head, "source_index": source_index, "real_cli": real_cli}


def run(agentctl, claude, model="claude-haiku-5-5"):
    agentctl, claude = Path(agentctl).resolve(), Path(claude).resolve()
    if not agentctl.is_file() or not os.access(agentctl, os.X_OK):
        raise ValueError("Installed agentctl executable is unavailable.")
    if not claude.is_file() or not os.access(claude, os.X_OK):
        raise ValueError("Claude executable is unavailable.")
    if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9._-]+", model):
        raise ValueError("Invalid model label.")
    evidence_root = Path(tempfile.mkdtemp(prefix="agent-handoff-provider-interaction-"))
    evidence_root.chmod(0o700)
    pointer = Path("/tmp/agent-handoff-provider-interaction-evidence.pointer")
    failure_category = "fixture_setup"
    try:
        paths = _fixture(evidence_root, agentctl)
        source, remote, private_root, wrapper_dir = paths["source"], paths["remote"], paths["actor_private"], paths["bin_dir"]
        config_path, log_path = evidence_root / "proxy-config.json", evidence_root / "proxy-calls.jsonl"
        for path in (log_path, evidence_root / "turn1.jsonl", evidence_root / "turn1.stderr",
                     evidence_root / "turn2.jsonl", evidence_root / "turn2.stderr"):
            write_private(path, b"")
        config = {"real_cli": paths["real_cli"], "authority": str(remote), "ref": paths["authority"].publication_ref,
                  "private_root": str(private_root), "source": str(source), "managed": None,
                  "log": str(log_path), "verify_store": str(evidence_root / "proxy-verify.git"),
                  "stage": "discovery", "inspection_succeeded": False, "context_created": False,
                  "approved": False, "decision_id": None, "prior_owner_revision": paths["owner_revision"],
                  "prior_owner_session": paths["owner_session_id"],
                  "initial_source_head": paths["source_head"],
                  "initial_source_index_sha256": paths["source_index"],
                  "mainline_head": paths["base"]}
        write_private(config_path, json.dumps(config, separators=(",", ":")).encode())
        make_proxy(wrapper_dir / "agentctl", config_path)
        before_settings = provider_settings_snapshot("claude")
        write_private(evidence_root / "claude-settings-before.json", json.dumps(before_settings).encode())
        setup = ("Approved setup facts: agentctl is available on PATH at " + str(wrapper_dir / "agentctl") +
                 ". Approved authority: " + str(remote) + ". Approved full publication ref: " +
                 paths["authority"].publication_ref + ". Approved private state root: " + str(private_root) + ".\n")
        first_prompt = setup + "Follow the project instructions and change answer() in app.py to return 42.\n"
        failure_category = "first_provider_turn"
        first, raw1, native1, normalized1, err1 = _run_turn(claude, model, remote, private_root, source,
            wrapper_dir, evidence_root / "turn1.jsonl", evidence_root / "turn1.stderr", first_prompt)
        if first.returncode != 0 or provider_settings_snapshot("claude") != before_settings:
            raise EvidenceError("first_turn_exit_or_settings_guard_failed")
        actor_token = read_context(private_root, source)
        calls1 = parse_log(log_path)
        first_proof = verify_first_turn(normalized1, calls1, source, actor_token, native1)
        prose = first_proof.pop("assistant_prose")
        write_private(evidence_root / "first-assistant-prose.txt", prose.encode("utf-8"))
        if not first_proof["choice_terms_detected_for_manual_review"]:
            raise EvidenceError("first_turn_did_not_present_detectable_choice_terms")
        head_after_first, owner_after_first = _remote_owner(config)
        if (head_after_first != paths["owner_revision"] or not owner_after_first or
                owner_after_first["session_id"] != paths["owner_session_id"] or
                git("-C", str(source), "rev-parse", "HEAD") != paths["source_head"] or
                digest((source / ".git/index").read_bytes()) != paths["source_index"] or
                (source / "app.py").read_text(encoding="utf-8") != "def answer():\n    return 41\n"):
            raise EvidenceError("first_turn_changed_owner_or_source")
        decision_id = first_proof["decision_id"]
        config = _read_json(config_path)
        if decision_id != config.get("decision_id"):
            raise EvidenceError("decision_identity_not_logged")
        # This is the explicit simulated user response between two terminated sessions.
        set_user_approval(config_path, decision_id, True)
        second_prompt = ("Previous assistant's natural-language decision presentation:\n" + prose +
            "\nPending public runtime decision:\n" + json.dumps(config["pending_public_decision"]) +
            "\nUser choice for the pending runtime decision " + decision_id +
            ": choose the takeover option. Continue the same task. The existing private context file is " +
            str(private_root / "context.json") + ".\n" + setup +
            "Follow the project instructions and change answer() in app.py to return 42.\n")
        failure_category = "second_provider_turn"
        second, raw2, native2, normalized2, err2 = _run_turn(claude, model, remote, private_root, source,
            wrapper_dir, evidence_root / "turn2.jsonl", evidence_root / "turn2.stderr", second_prompt)
        if second.returncode != 0 or provider_settings_snapshot("claude") != before_settings:
            raise EvidenceError("second_turn_exit_or_settings_guard_failed")
        if any(actor_token.encode() in data for data in (raw1, raw2, err1, err2)):
            raise EvidenceError("context_token_leak")
        calls2 = parse_log(log_path)[len(calls1):]
        config = _read_json(config_path)
        managed = Path(config.get("managed", "")).resolve()
        second_proof = verify_second_turn(normalized2, calls2, managed, actor_token, decision_id)
        if (config.get("stage") != "ready" or config.get("applied_decision_id") != decision_id or
                config.get("applied_option_id") != "takeover"):
            raise EvidenceError("proxy_state_machine_not_complete")
        expected_revision = config.get("resume_revision")
        state_proof = verify_final_state(source, managed,
            GitBackend(evidence_root / "verify-final.git", str(remote), paths["authority"].publication_ref),
            paths["mainline"], paths["base"], paths["source_head"], expected_revision)
        owner_revision = verify_owner(private_root, managed, paths["authority"], actor_token,
            GitBackend(evidence_root / "verify-capability.git", str(remote), paths["authority"].publication_ref))
        owner = GitBackend(evidence_root / "verify-final-owner.git", str(remote), paths["authority"].publication_ref)
        observed_owner_revision = owner.observe()
        if observed_owner_revision != owner_revision:
            raise EvidenceError("final_owner_revision_not_current")
        current_owner = owner.records(observed_owner_revision)["owner"]["owner"]
        if (observed_owner_revision != expected_revision or current_owner["session_id"] == paths["owner_session_id"] or
                current_owner["session_id"] != config.get("actor_session") or
                digest((source / ".git/index").read_bytes()) != paths["source_index"]):
            raise EvidenceError("takeover_session_or_source_proof_failed")
        if raw1.find(actor_token.encode()) >= 0 or raw2.find(actor_token.encode()) >= 0 or err1.find(actor_token.encode()) >= 0 or err2.find(actor_token.encode()) >= 0:
            raise EvidenceError("context_token_leak")
        version = subprocess.run([str(claude), "--version"], capture_output=True, timeout=15, umask=0o077)
        version_text = version.stdout.decode("utf-8", errors="ignore").strip().splitlines()
        _write_json(evidence_root / "interaction-result.json", {
            "mechanical_scenario_passed": True, "agent_interaction_verified": False,
            "primary_prose_review_required": True, "choice_terms_detected_for_manual_review": True,
            "first_turn_exit_code": first.returncode, "second_turn_exit_code": second.returncode,
            "model": model, "provider_version": version_text[0][:120] if version.returncode == 0 and version_text else None,
            "provider": "claude", "host": "Ubuntu/WSL current process", "git_version": git("--version"),
            "first_trace_sha256": digest(raw1), "second_trace_sha256": digest(raw2),
            "takeover_decision_applied": True, "takeover_new_session": True,
            "owner_capability_proved": True, "source_unchanged": state_proof["source_unchanged"],
            "managed_app_returns_42": state_proof["managed_app_returns_42"],
            "no_feature_publication": state_proof["no_feature_publication"],
            "mainline_unchanged": state_proof["mainline_unchanged"],
            "provider_settings_and_auth_unchanged": True,
            "provider_settings_guard_scope": "settings.json/.credentials.json",
            "context_token_absent_from_traces_and_stderr": True,
            "wrapper_phases": [{key: entry.get(key) for key in ("phase", "result_command", "status", "exit_code")}
                               for entry in parse_log(log_path)],
            **first_proof, **second_proof,
        })
        write_private(pointer, (str(evidence_root) + "\n").encode())
        return {"mechanical_scenario_passed": True, "agent_interaction_verified": False,
                "primary_prose_review_required": True, "choice_terms_detected_for_manual_review": True,
                "provider": "claude", "model": model,
                "provider_version": version_text[0][:120] if version.returncode == 0 and version_text else None,
                "first_trace_sha256": digest(raw1), "second_trace_sha256": digest(raw2),
                "takeover_new_session": True, "owner_capability_proved": True,
                "source_unchanged": state_proof["source_unchanged"],
                "managed_app_returns_42": state_proof["managed_app_returns_42"],
                "no_feature_publication": state_proof["no_feature_publication"],
                "mainline_unchanged": state_proof["mainline_unchanged"],
                "provider_settings_and_auth_unchanged": True,
                "provider_settings_guard_scope": "settings.json/.credentials.json",
                "context_token_absent_from_traces_and_stderr": True,
                "private_evidence_pointer_written": True,
                "evidence_scope": "two actual fresh Claude turns with explicit simulated takeover choice on isolated local Git"}
    except EvidenceError as error:
        write_private(pointer, (str(evidence_root) + "\n").encode())
        return {"mechanical_scenario_passed": False, "agent_interaction_verified": False,
                "primary_prose_review_required": True, "failure_category": failure_category,
                "evidence": str(error), "private_evidence_pointer_written": True}
    except Exception:
        write_private(pointer, (str(evidence_root) + "\n").encode())
        return {"mechanical_scenario_passed": False, "agent_interaction_verified": False,
                "primary_prose_review_required": True, "failure_category": failure_category,
                "evidence": "unexpected_private_failure", "private_evidence_pointer_written": True}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--agentctl", type=Path, required=True)
    parser.add_argument("--claude", type=Path, required=True)
    parser.add_argument("--model", default="claude-haiku-5-5")
    args = parser.parse_args()
    report = run(args.agentctl, args.claude, args.model)
    print(json.dumps(report, sort_keys=True))
    return 0 if report.get("mechanical_scenario_passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
