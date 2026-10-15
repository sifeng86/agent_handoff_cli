"""Separate-process hosted checkpoint resume and checkout-preservation acceptance."""
import argparse
import hashlib
import json
from pathlib import Path
import platform
import re
import subprocess
import sys
import tempfile
import uuid


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_handoff.authority import Authority
from agent_handoff.git_backend import GitBackend, git_environment
from agent_handoff.paths import PrivateStore
from agent_handoff.plans import parse as parse_plan
from agent_handoff.public_results import validate_public_result


CLI_SOURCE = "from agent_handoff.cli import main; raise SystemExit(main())"
STATE = {"schema_version": 1, "status": "working", "active_plan": "P-RESUME", "current_step": "S-001",
         "blocker": None, "summary": ""}
PLAN = {"schema_version": 1, "id": "P-RESUME", "status": "READY", "goal": "Verify hosted resume",
        "scope": "Selected text and binary files", "non_goals": "Credentials and provider data",
        "steps": ["S-001", "S-002"], "acceptance_criteria": "Resume recovers the exact committed snapshot"}


def git(*args, timeout=30):
    try:
        process = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *args], env=git_environment(),
                                 capture_output=True, timeout=timeout, umask=0o077)
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError("Git operation failed or timed out.") from None
    if process.returncode:
        raise ValueError("Git operation failed.")
    return process.stdout.decode("utf-8", errors="strict").strip()


def cli(checkout, private_root, context_file, authority, publication_ref, *args):
    source_root = str(Path(__file__).resolve().parents[1] / "src")
    env = git_environment()
    env["PYTHONPATH"] = source_root
    command = [sys.executable, "-c", CLI_SOURCE, *args,
               "--authority", authority, "--publication-ref", publication_ref,
               "--checkout", str(checkout), "--private-root", str(private_root)]
    if context_file is not None:
        command.extend(("--context-file", str(context_file)))
    try:
        process = subprocess.run(command, cwd=checkout, env=env, capture_output=True, timeout=180, umask=0o077)
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError("CLI phase failed or timed out.") from None
    try:
        report = json.loads(process.stdout.decode("utf-8"))
        validate_public_result(report)
    except Exception:
        raise ValueError("CLI phase returned an invalid public result.") from None
    if process.returncode not in range(0, 9):
        raise ValueError("CLI phase returned an invalid exit status.")
    return report, process.returncode


def source_evidence(checkout):
    paths = ("selected-resume.txt", "selected-resume.bin", "after-checkpoint.bin", ".env",
             ".gitignore", "plans/P-RESUME.md")
    contents = {}
    for name in paths:
        path = checkout / name
        contents[name] = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
    return {"head": git("-C", str(checkout), "rev-parse", "HEAD"),
            "index": hashlib.sha256((checkout / ".git/index").read_bytes()).hexdigest(),
            "status": hashlib.sha256(subprocess.check_output(
                ["git", "-C", str(checkout), "-c", "core.hooksPath=/dev/null", "status", "--porcelain=v1", "-z",
                 "--untracked-files=all", "--ignored=matching"], env=git_environment(), timeout=30)).hexdigest(),
            "contents": contents}


def private_session(private_root, checkout, authority):
    root = private_root / "repos" / authority.identity
    with PrivateStore(root, checkout) as store:
        binding = store.read("checkout-binding.json")
        session = store.read("session-" + binding["session_id"] + ".json")
        if session.get("session_id") != binding["session_id"]:
            raise ValueError("Private session binding is inconsistent.")
        return session


def verify_owner(backend, revision, session):
    owner = backend.records(revision)["owner"]["owner"]
    if (not isinstance(owner, dict) or
            set(owner) != {"session_id", "acquisition_operation_id", "machine_label", "provider_label"} or
            owner["session_id"] != session["session_id"] or
            owner["acquisition_operation_id"] != session["acquisition_operation_id"]):
        raise ValueError("Published owner does not match the private session proof.")


def verify_parent(backend, revision, parent):
    if backend.observe() != revision:
        raise ValueError("Published revision differs from the exact remote ref head.")
    if backend.text("rev-list", "--parents", "-n", "1", revision).split() != [revision, parent]:
        raise ValueError("Publication did not create the expected single-parent commit.")


def run(destination, mainline, base):
    if not isinstance(base, str) or not re.fullmatch(r"[0-9a-f]{40}", base):
        raise ValueError("Explicit SHA-1 base is required.")
    if not isinstance(mainline, str) or not mainline.startswith("refs/heads/"):
        raise ValueError("Explicit full mainline ref is required.")
    ref = "refs/heads/agent-handoff-resume-smoke-" + uuid.uuid4().hex
    authority = Authority(destination, ref)
    branch_deleted = False
    try:
        with tempfile.TemporaryDirectory(prefix="agent-handoff-resume-hosting-") as temporary:
            root = Path(temporary)
            checkout = root / "source"
            private_root = root / "private"
            private_root.mkdir(mode=0o700)
            observer = GitBackend(root / "observer.git", destination, ref)
            mainline_backend = GitBackend(root / "mainline.git", destination, mainline)
            if mainline_backend.remote_head() != base:
                raise ValueError("Mainline must match the explicit base.")
            mainline_before = mainline_backend.remote_head()
            git("clone", "--quiet", "--no-checkout", "--branch", mainline[len("refs/heads/"):],
                "--", destination, str(checkout))
            git("-C", str(checkout), "checkout", "--quiet", "--detach", base)
            context_result, context_code = cli(checkout, private_root, None, destination, ref, "context", "create")
            if context_code != 0 or context_result["command"] != "context create":
                raise ValueError("Private context creation failed.")
            context_file = private_root / "context.json"
            if not context_file.is_file():
                raise ValueError("Private runtime context was not created.")
            initialized, init_code = cli(checkout, private_root, context_file, destination, ref,
                                         "init", "--base", base, "--mainline-ref", mainline)
            if init_code != 0 or initialized.get("code_publication") != "published":
                raise ValueError("Publication initialization failed.")
            init_revision = initialized["published_revision"]
            verify_parent(observer, init_revision, base)
            acquired, acquire_code = cli(checkout, private_root, context_file, destination, ref, "acquire")
            if acquire_code != 0 or acquired.get("ownership") != "owned":
                raise ValueError("Initial writer acquisition failed.")
            acquire_revision = acquired["published_revision"]
            verify_parent(observer, acquire_revision, init_revision)
            session = private_session(private_root, checkout, authority)

            (checkout / ".gitignore").write_text(".env\n", encoding="utf-8")
            plan_dir = checkout / "plans"
            plan_dir.mkdir(exist_ok=True)
            plan_text = "<!-- agent-handoff-plan\n" + json.dumps(PLAN, sort_keys=True) + "\n-->\n# Resume Plan\n"
            (plan_dir / "P-RESUME.md").write_text(plan_text, encoding="utf-8")
            selected_text = "committed text snapshot\n"
            selected_binary = b"committed binary\x00snapshot\xff\n"
            (checkout / "selected-resume.txt").write_text(selected_text, encoding="utf-8")
            (checkout / "selected-resume.bin").write_bytes(selected_binary)
            state_file = private_root / "resume-state.json"
            state_file.write_text(json.dumps(STATE), encoding="utf-8")
            before_checkpoint_head = git("-C", str(checkout), "rev-parse", "HEAD")
            before_checkpoint_index = (checkout / ".git/index").read_bytes()
            checkpoint, checkpoint_code = cli(checkout, private_root, context_file, destination, ref,
                "checkpoint", "--summary", "Hosted snapshot at S-001", "--state-file", str(state_file),
                "--include", "plans/P-RESUME.md", "--include", "selected-resume.txt", "--include", "selected-resume.bin")
            if checkpoint_code != 0 or checkpoint.get("ownership") != "owned":
                raise ValueError("Checkpoint publication failed.")
            checkpoint_revision = checkpoint["published_revision"]
            verify_parent(observer, checkpoint_revision, acquire_revision)
            if (git("-C", str(checkout), "rev-parse", "HEAD") != before_checkpoint_head or
                    (checkout / ".git/index").read_bytes() != before_checkpoint_index):
                raise ValueError("Checkpoint changed source HEAD or index.")

            (checkout / "selected-resume.txt").write_text("staged source edit\n", encoding="utf-8")
            git("-C", str(checkout), "add", "--", "selected-resume.txt")
            (checkout / "selected-resume.txt").write_text("unstaged source edit\n", encoding="utf-8")
            (checkout / "after-checkpoint.bin").write_bytes(b"untracked local bytes\x00\xfe")
            (checkout / ".env").write_text("LOCAL_DUMMY=never-publish\n", encoding="utf-8")
            source_before_resume = source_evidence(checkout)
            resume, resume_code = cli(checkout, private_root, context_file, destination, ref, "resume")
            if (resume_code != 6 or resume.get("status") != "not_ready" or resume.get("ownership") != "owned" or
                    resume.get("next_action") != "Coding Agent must integrate preserved local edits before continuing."):
                raise ValueError("Resume did not preserve the integration-required ownership gate.")
            if resume.get("observed_revision") != checkpoint_revision or observer.remote_head() != checkpoint_revision:
                raise ValueError("Resume changed or misidentified the checkpoint revision.")
            if private_session(private_root, checkout, authority)["session_id"] != session["session_id"]:
                raise ValueError("Resume changed the active private session identity.")
            managed = Path(resume["managed_checkout"]).resolve()
            if (managed == checkout.resolve() or not managed.is_dir() or
                    not managed.is_relative_to((private_root / "repos").resolve())):
                raise ValueError("Resume did not return a private managed checkout.")
            if git("-C", str(managed), "rev-parse", "HEAD") != checkpoint_revision:
                raise ValueError("Managed checkout HEAD differs from the committed snapshot.")
            verify_owner(observer, checkpoint_revision, session)
            if ((managed / "selected-resume.txt").read_text(encoding="utf-8") != selected_text or
                    (managed / "selected-resume.bin").read_bytes() != selected_binary or
                    (managed / "plans/P-RESUME.md").read_text(encoding="utf-8") != plan_text or
                    parse_plan((managed / "plans/P-RESUME.md").read_text(encoding="utf-8"))["id"] != "P-RESUME" or
                    json.loads((managed / ".agent/state.json").read_text(encoding="utf-8")) !=
                    dict(STATE, summary="Hosted snapshot at S-001")):
                raise ValueError("Managed checkout does not match the exact committed files and state.")
            if (managed / "after-checkpoint.bin").exists() or (managed / ".env").exists():
                raise ValueError("Post-checkpoint local files leaked into the managed checkout.")
            source_after_resume = source_evidence(checkout)
            if source_after_resume != source_before_resume:
                raise ValueError("Resume modified the original checkout or ignored local file.")

            managed_recovery, managed_recovery_code = cli(managed, private_root, context_file, destination, ref,
                                                          "recover-session")
            if (managed_recovery_code != 0 or managed_recovery.get("ownership") != "owned" or
                    managed_recovery.get("observed_revision") != checkpoint_revision or
                    observer.remote_head() != checkpoint_revision or
                    private_session(private_root, managed, authority)["session_id"] != session["session_id"]):
                raise ValueError("Managed checkout could not prove the existing session without publication.")
            fenced, fenced_code = cli(checkout, private_root, context_file, destination, ref, "recover-session")
            if (fenced_code != 4 or fenced.get("status") != "rejected" or fenced.get("ownership") != "unknown" or
                    observer.remote_head() != checkpoint_revision or source_evidence(checkout) != source_before_resume):
                raise ValueError("Original checkout was not rejected without changing local or remote state.")
            resumed_again, resume_again_code = cli(managed, private_root, context_file, destination, ref, "resume")
            if (resume_again_code != 0 or resumed_again.get("status") != "ready" or
                    resumed_again.get("managed_checkout") != str(managed) or
                    resumed_again.get("observed_revision") != checkpoint_revision or
                    observer.remote_head() != checkpoint_revision or
                    private_session(private_root, managed, authority)["session_id"] != session["session_id"]):
                raise ValueError("Managed checkout resume was not stable and read-only.")
            released, release_code = cli(managed, private_root, context_file, destination, ref, "release")
            if release_code != 0 or released.get("ownership") != "released":
                raise ValueError("Managed checkout release failed.")
            final_revision = released["published_revision"]
            verify_parent(observer, final_revision, checkpoint_revision)
            if observer.records(final_revision)["owner"]["owner"] is not None:
                raise ValueError("Final release did not clear the publication owner.")
            if source_evidence(checkout) != source_before_resume:
                raise ValueError("Managed release changed the original source checkout.")
            if mainline_backend.remote_head() != mainline_before or mainline_before != base:
                raise ValueError("Mainline changed during resume acceptance.")
            if observer.remote_head() != final_revision:
                raise ValueError("Publication branch changed unexpectedly before cleanup.")
            deleted = git("push", "--porcelain", "--force-with-lease=" + ref + ":" + final_revision,
                          "--", destination, ":" + ref)
            if "-\t:" + ref not in deleted and "\t:" + ref not in deleted:
                raise ValueError("Publication branch deletion was not confirmed.")
            branch_deleted = observer.remote_head() is None
            if not branch_deleted or mainline_backend.remote_head() != mainline_before:
                raise ValueError("Branch cleanup or final mainline check failed.")
            return {"passed": True, "publication_ref": ref, "branch_deleted": True,
                    "mainline_unchanged": True, "mainline_revision": base,
                    "git_version": git("--version"), "python_version": platform.python_version(),
                    "provider": "git-remote", "provider_version": None,
                    "initial_revision": init_revision, "acquire_revision": acquire_revision,
                    "checkpoint_revision": checkpoint_revision, "final_revision": final_revision,
                    "init_status": initialized["status"], "acquire_status": acquired["status"],
                    "checkpoint_status": checkpoint["status"], "resume_status": resume["status"],
                    "managed_recovery_status": managed_recovery["status"],
                    "original_recovery_status": fenced["status"], "release_status": released["status"],
                    "managed_resume_status": resumed_again["status"],
                    "managed_checkout_private": True, "resume_snapshot_exact": True,
                    "preserved_edits_require_integration": True,
                    "source_state_preserved": True, "recover_session_proved": True,
                    "original_checkout_fenced": True, "resume_idempotent": True,
                    "A12": True, "A25": True, "A26": True}
    except Exception:
        return {"passed": False, "publication_ref": ref, "branch_deleted": branch_deleted,
                "mainline_unchanged": False, "error": "Hosting resume smoke failed; private details were withheld."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--authority", required=True)
    parser.add_argument("--mainline-ref", required=True)
    parser.add_argument("--base", required=True)
    args = parser.parse_args()
    report = run(args.authority, args.mainline_ref, args.base)
    print(json.dumps(report))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
