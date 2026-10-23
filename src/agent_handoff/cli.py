"""JSON-only CLI with explicit authority, context and backend capability gates."""
import argparse
import json
import sys
import os
from pathlib import Path

from .contracts import RuntimeFailure
from .git_backend import inspect
from .output import result, exit_code
from .public_results import PublicResultFailure


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise RuntimeFailure("Invalid command arguments; see agentctl --help.")


def secret_service(runtime, args, *, candidate=None):
    from .secrets.service import SecretService
    from .secrets.manifest import parse as parse_manifest
    from .secrets.doppler import Doppler
    binding = candidate or runtime.store.read("secret-binding.json", missing=True)
    manifest_path = args.manifest_file
    default = runtime.checkout / ".agent/secrets.example.yaml"
    if manifest_path is None and default.exists():
        manifest_path = default
    if manifest_path is not None:
        manifest = parse_manifest(manifest_path.read_text(encoding="utf-8"))
    elif binding is not None:
        manifest = binding["manifest"]
    else:
        raise RuntimeFailure("An explicit non-secret --manifest-file is required before secret configuration.")
    mode = args.secrets_mode or (binding or {}).get("mode")
    project = args.doppler_project or (binding or {}).get("project")
    config = args.doppler_config or (binding or {}).get("config")
    if binding is not None and candidate is None and args.action != "configure":
        if (mode, project, config) != (binding["mode"], binding["project"], binding["config"]):
            raise RuntimeFailure("Secret destination or mode changed; propose a new binding decision.", "decision_required", 3)
    adapter = Doppler(project, config, runtime.checkout) if mode == "doppler" else None
    return SecretService(runtime, manifest, adapter)


def main(argv=None) -> int:
    parser = Parser(prog="agentctl")
    parser.add_argument("command", choices=("status", "inspect", "init", "acquire", "recover-session",
                                           "takeover", "checkpoint", "handoff", "release", "done",
                                           "decision", "operation", "context", "resume", "secrets", "host", "authority", "recover-ownership"))
    parser.add_argument("action", nargs="?")
    parser.add_argument("--authority")
    parser.add_argument("--publication-ref")
    parser.add_argument("--target-authority")
    parser.add_argument("--target-publication-ref")
    parser.add_argument("--mainline-ref")
    parser.add_argument("--base")
    parser.add_argument("--checkout", type=Path, default=Path.cwd())
    parser.add_argument("--private-root", type=Path,
                        default=Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "agent-handoff")
    parser.add_argument("--context-file", type=Path)
    parser.add_argument("--decision-id")
    parser.add_argument("--option-id")
    parser.add_argument("--operation-id")
    parser.add_argument("--include", action="append", default=[])
    parser.add_argument("--summary")
    parser.add_argument("--state-file", type=Path)
    parser.add_argument("--release", choices=("true", "false"))
    parser.add_argument("--manifest-file", type=Path)
    parser.add_argument("--secrets-mode", choices=("local-env", "doppler"))
    parser.add_argument("--doppler-project")
    parser.add_argument("--doppler-config")
    parser.add_argument("--grant-upload", action="store_true")
    parser.add_argument("--plan-id")
    parser.add_argument("--patch-file", type=Path)
    parser.add_argument("--preservation-handle")
    parser.add_argument("--host-file", type=Path)
    parser.add_argument("--backup-handle")
    parser.add_argument("--expected-handle")
    command = "unknown"
    value = result(command, status="not_ready")
    code = 0
    expected_family = None
    try:
        args = parser.parse_args(argv)
        expected_family = args.command if args.command in ("host", "authority", "inspect", "status", "context", "resume", "secrets") else None
        if args.command in ("init", "acquire", "recover-session", "checkpoint", "handoff", "done", "release", "operation"):
            expected_family = "git"
        if args.command == "decision" and args.action == "propose-integrity":
            expected_family = "decision-proposal"
        value["command"] = args.command
        if args.action is not None and args.command not in {"context", "host", "authority", "secrets", "decision", "operation"}:
            raise RuntimeFailure("This command does not accept a positional action; see agentctl --help.")
        if sys.platform != "linux":
            raise RuntimeFailure("Only Linux/WSL runtime is supported.", "unsupported", 8)
        if args.command == "recover-ownership":
            raise RuntimeFailure("recover-ownership was removed; use recover-session or an approved takeover decision.")
        from .paths import PrivateStore
        if args.command == "context":
            if args.action != "create":
                raise RuntimeFailure("Use context create to establish a private runtime context.")
            import secrets
            with PrivateStore(args.private_root, args.checkout) as store:
                if store.read("context.json", missing=True) is not None:
                    raise RuntimeFailure("Context already exists; use its private file explicitly.", "rejected", 4)
                store.write("context.json", {"schema_version": 1, "context": secrets.token_hex(32)})
            value = result("context create", context_file=str(args.private_root.resolve() / "context.json"))
        elif args.command == "host":
            from .host_guard import HostGuard
            from .host_recovery import HostFileRecovery
            with PrivateStore(args.private_root / "host", args.checkout) as store:
                guard = HostGuard(store)
                if args.action == "inspect":
                    value = result("host inspect", **guard.inspect())
                elif args.action == "verify" and args.preservation_handle is not None:
                    value = result("host verify", **guard.verify(args.preservation_handle))
                elif args.action == "backup" and args.host_file is not None:
                    value = result("host backup", **HostFileRecovery(store).backup(args.host_file))
                elif args.action == "verify-backup" and args.backup_handle is not None:
                    HostFileRecovery(store).read(args.backup_handle)
                    value = result("host verify-backup", backup_handle=args.backup_handle, backup_verified=True)
                elif args.action == "restore" and args.backup_handle is not None and args.expected_handle is not None:
                    value = result("host restore", **HostFileRecovery(store).restore(args.backup_handle, args.expected_handle))
                elif args.action == "resolve" and args.operation_id is not None:
                    value = result("host resolve", **HostFileRecovery(store).resolve(args.operation_id))
                else:
                    raise RuntimeFailure("Use host inspect/verify, backup/verify-backup, restore with both handles, or resolve with an operation ID.")
        elif args.command == "authority" and args.action == "resolve":
            if args.context_file is None:
                raise RuntimeFailure("Use authority resolve with an explicit --context-file.")
            from .authority_selection import AuthoritySelection
            from .sessions import checkout_identity, context_identity
            with PrivateStore(args.context_file.absolute().parent, args.checkout) as store:
                context = store.read(args.context_file.name).get("context")
            with PrivateStore(args.private_root / "selections", args.checkout) as store:
                selection = AuthoritySelection(store, checkout_identity(args.checkout), context_identity(context))
                with selection.locked():
                    from .authority_transition import finalize_recovery
                    resolved = selection.resolve(finalize=lambda journal, outcome:
                        finalize_recovery(args.private_root, args.checkout.resolve(), journal, outcome))
                value = result("authority resolve", transition_id=resolved["transition_id"],
                               transition_outcome=resolved["outcome"],
                               selected_authority_identity=resolved["after" if resolved["outcome"] == "selected" else "before"]["repository_identity"])
        elif not args.authority or not args.publication_ref:
            raise RuntimeFailure("Explicit --authority and --publication-ref are required.")
        elif args.command in ("status", "inspect"):
            value = result(args.command, inspection=inspect(args.authority, args.publication_ref))
        else:
            from .authority import Authority
            from .publication import Runtime
            context = None
            if args.context_file is not None:
                with PrivateStore(args.context_file.absolute().parent, args.checkout) as store:
                    context = store.read(args.context_file.name).get("context")
                if not isinstance(context, str):
                    raise RuntimeFailure("Invalid private runtime context.")
            with Runtime(args.checkout, args.private_root, Authority(args.authority, args.publication_ref), context) as runtime:
                if args.command == "authority":
                    if args.action not in ("propose", "apply") or not args.target_authority or not args.target_publication_ref:
                        raise RuntimeFailure("Use authority propose/apply with explicit target authority and publication ref.")
                    from .authority_transition import AuthorityTransition
                    with Runtime(args.checkout, args.private_root,
                                 Authority(args.target_authority, args.target_publication_ref), context) as target:
                        transition = AuthorityTransition(runtime, target)
                        if args.action == "propose":
                            value = transition.propose()
                        elif args.decision_id is None or args.option_id is None:
                            raise RuntimeFailure("An explicit decision ID and option ID are required.")
                        else:
                            value = transition.apply(args.decision_id, args.option_id)
                elif args.command == "init":
                    value = runtime.initialize(args.base, mainline_ref=args.mainline_ref)
                elif args.command == "acquire":
                    value = runtime.acquire()
                elif args.command == "recover-session":
                    value = runtime.recover()
                elif args.command == "release":
                    value = runtime.release()
                elif args.command == "resume":
                    value = runtime.resume()
                elif args.command == "secrets":
                    service = secret_service(runtime, args)
                    if args.action == "configure":
                        if args.secrets_mode is None:
                            raise RuntimeFailure("Secret configuration requires an explicit --secrets-mode.")
                        value = service.propose_binding(args.secrets_mode, project=args.doppler_project,
                            config=args.doppler_config, upload_authorized=args.grant_upload)
                    elif args.action == "inspect":
                        value = service.inspect()
                    elif args.action == "verify":
                        value = service.verify()
                    elif args.action == "reconcile":
                        value = service.reconcile()
                    elif args.action == "apply":
                        if args.patch_file is not None and args.plan_id is not None:
                            raise RuntimeFailure("Choose exactly one private patch or reconciliation plan.")
                        if args.patch_file is not None:
                            with PrivateStore(args.patch_file.absolute().parent, args.checkout) as store:
                                private_patch = store.read(args.patch_file.name)
                            value = service.apply_local(private_patch)
                        elif args.plan_id is not None:
                            value = service.apply(args.plan_id)
                        else:
                            raise RuntimeFailure("Secret apply requires a private --patch-file or --plan-id.")
                    elif args.action == "resolve" and args.operation_id is not None:
                        value = service.resolve(args.operation_id)
                    else:
                        raise RuntimeFailure("Use secrets configure/inspect/reconcile/apply/verify/resolve.")
                elif args.command in ("checkpoint", "handoff", "done"):
                    if args.summary is None:
                        raise RuntimeFailure("An explicit --summary is required.")
                    state = None
                    if args.state_file is not None:
                        from .contracts import parse_record
                        state = parse_record(args.state_file.read_text(encoding="utf-8"), "state")
                    sync = None
                    if args.command == "handoff" and runtime.store.read("secret-binding.json", missing=True) is not None:
                        sync = secret_service(runtime, args)
                    value = runtime.publish_snapshot(args.command, args.include, summary=args.summary, state=state,
                        release_intent=None if args.release is None else args.release == "true", secret_service=sync)
                elif args.command in ("decision", "takeover"):
                    if args.command == "decision" and args.action == "propose-integrity":
                        value = runtime.propose_integrity()
                    else:
                        if args.command == "decision" and args.action != "apply":
                            raise RuntimeFailure("Only decision apply is supported.")
                        if args.decision_id is None or args.option_id is None:
                            raise RuntimeFailure("An explicit decision ID and option ID are required.")
                        from .secrets.decision_records import read as read_private_decision
                        private = read_private_decision(runtime.store, args.decision_id, missing=True)
                        if private is not None:
                            candidate = private["candidate"] if private["kind"] == "binding" else None
                            value = secret_service(runtime, args, candidate=candidate).apply_decision(args.decision_id, args.option_id)
                        else:
                            value = runtime.apply_decision(args.decision_id, args.option_id)
                elif args.command == "operation":
                    if args.action != "resolve" or args.operation_id is None:
                        raise RuntimeFailure("Use operation resolve with an explicit operation ID.")
                    value = runtime.resolve(args.operation_id)
        code = exit_code(value)
    except PublicResultFailure as failure:
        if expected_family in ("git", "resume"):
            value = result("operation", status="invalid", code_publication="publication_unknown", ownership="ownership_pending",
                           next_action="Unsafe public output was rejected; inspect durable publication evidence before retrying.")
        elif expected_family == "secrets":
            from .public_results import identifier
            operation_id = getattr(failure, "secret_operation_id", None)
            fields = {"secret_operation_id": operation_id} if identifier(operation_id) else {}
            value = result("secrets resolve" if fields else "secrets", status="invalid", secrets_shared="unknown",
                           next_action="Unsafe secret output was rejected; inspect durable evidence before retrying.", **fields)
        else:
            value = result("decision" if expected_family == "decision-proposal" else expected_family or "unknown", status="invalid", next_action="Unsafe public output was rejected; preserve private evidence.")
        code = 7
    except RuntimeFailure as exc:
        value.update(status=exc.status, next_action=str(exc))
        code = exc.code
    except (OSError, UnicodeError, ValueError):
        value.update(status="not_ready", next_action="Runtime input or private storage is unavailable.")
        code = 6
    except Exception:
        value.update(status="not_ready", next_action="Unexpected runtime failure; preserve private evidence and inspect before retrying.")
        code = 6
    from .public_results import validate_public_result
    try:
        validate_public_result(value, expected_family=expected_family)
    except RuntimeFailure:
        if expected_family in ("git", "resume"):
            operation_id = value.get("operation_id")
            from .public_results import identifier
            value = result("operation", status="invalid", code_publication="publication_unknown", ownership="ownership_pending",
                           operation_id=operation_id if identifier(operation_id) else None,
                           next_action="Unsafe public output was rejected; inspect durable publication evidence before retrying.")
        elif expected_family == "secrets":
            from .public_results import identifier
            operation_id = value.get("secret_operation_id")
            fields = {"secret_operation_id": operation_id} if identifier(operation_id) else {}
            value = result("secrets resolve" if fields else "secrets", status="invalid", secrets_shared="unknown",
                           next_action="Unsafe secret output was rejected; inspect durable evidence before retrying.", **fields)
        else:
            value = result("decision" if expected_family == "decision-proposal" else expected_family or "unknown", status="invalid", next_action="Unsafe public output was rejected; preserve private evidence.")
        code = 7
    print(json.dumps(value, ensure_ascii=True))
    return code


if __name__ == "__main__":
    sys.exit(main())
