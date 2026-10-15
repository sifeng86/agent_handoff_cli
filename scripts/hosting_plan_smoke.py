"""Plan preservation, takeover fencing, handoff, and completion acceptance."""
import argparse
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
from agent_handoff.contracts import RuntimeFailure
from agent_handoff.git_backend import GitBackend, git_environment
from agent_handoff.plans import parse as parse_plan
from agent_handoff.publication import Runtime


def git(*args, timeout=30):
    try:
        process = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *args],
                                 env=git_environment(), capture_output=True, timeout=timeout,
                                 umask=0o077)
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError("Git operation failed or timed out.") from None
    if process.returncode:
        raise ValueError("Git operation failed.")
    return process.stdout.decode("utf-8", errors="strict").strip()


def plan_text(status="READY"):
    metadata = {"schema_version": 1, "id": "P-SMOKE", "status": status, "goal": "Verify hosted Plan lifecycle",
                "scope": "A selected dummy file and Plan metadata", "non_goals": "Credentials and provider data",
                "steps": ["S-001", "S-002"], "acceptance_criteria": "All lifecycle states match their commits"}
    return "<!-- agent-handoff-plan\n" + json.dumps(metadata, sort_keys=True) + "\n-->\n# Hosting smoke Plan\n"


def state_record(status="working"):
    return {"schema_version": 1, "status": status, "active_plan": "P-SMOKE" if status != "idle" else None,
            "current_step": "S-001" if status != "idle" else None, "blocker": None, "summary": ""}


def assert_owner(records, session):
    owner = records["owner"]["owner"]
    if (not isinstance(owner, dict) or set(owner) != {"session_id", "acquisition_operation_id", "machine_label", "provider_label"} or
            owner["session_id"] != session["session_id"] or
            owner["acquisition_operation_id"] != session["acquisition_operation_id"]):
        raise ValueError("Published owner identity does not match the active session.")
    return owner


def verify_parent(backend, revision, parent):
    if backend.text("rev-list", "--parents", "-n", "1", revision).split() != [revision, parent]:
        raise ValueError("Publication did not create the expected single-parent commit.")
    if backend.remote_head() != revision:
        raise ValueError("Published revision differs from the exact remote ref head.")


def run(destination, mainline, base):
    if not re.fullmatch(r"[0-9a-f]{40}", base):
        raise ValueError("Explicit SHA-1 base is required.")
    if not isinstance(mainline, str) or not mainline.startswith("refs/heads/"):
        raise ValueError("Explicit full mainline ref is required.")
    ref = "refs/heads/agent-handoff-plan-smoke-" + uuid.uuid4().hex
    authority = Authority(destination, ref)
    final_revision = None
    branch_deleted = False
    takeover_new_session = False
    try:
        with tempfile.TemporaryDirectory(prefix="agent-handoff-plan-hosting-") as temporary:
            root = Path(temporary)
            observer = GitBackend(root / "observer.git", destination, ref)
            mainline_backend = GitBackend(root / "mainline.git", destination, mainline)
            if mainline_backend.remote_head() != base:
                raise ValueError("Mainline must match the explicit base.")
            mainline_before = mainline_backend.remote_head()
            checkouts = {}
            for label in ("writer-a", "writer-b"):
                path = root / label
                git("clone", "--quiet", "--no-checkout", "--branch", mainline[len("refs/heads/"):],
                    "--", destination, str(path))
                checkouts[label] = path
            contexts = {label: uuid.uuid4().hex + uuid.uuid4().hex for label in checkouts}
            private = {label: root / ("private-" + label) for label in checkouts}
            with Runtime(checkouts["writer-a"], private["writer-a"], authority, contexts["writer-a"]) as writer_a:
                initialized = writer_a.initialize(base, mainline_ref=mainline)
                if initialized["code_publication"] != "published":
                    raise ValueError("Initialization must create the expected-absent publication ref.")
                init_revision = initialized["published_revision"]
                verify_parent(writer_a.backend, init_revision, base)
                final_revision = init_revision
                acquired_a = writer_a.acquire()
                if acquired_a["ownership"] != "owned":
                    raise ValueError("Writer A must acquire the publication ref.")
                acquire_a_revision = acquired_a["published_revision"]
                verify_parent(writer_a.backend, acquire_a_revision, init_revision)
                session_a = writer_a.sessions.prove(writer_a.backend, acquire_a_revision,
                                                    writer_a.backend.records(acquire_a_revision))
                assert_owner(writer_a.backend.records(acquire_a_revision), session_a)
                plans = checkouts["writer-a"] / "plans"
                plans.mkdir(parents=True, exist_ok=True)
                (plans / "P-SMOKE.md").write_text(plan_text(), encoding="utf-8")
                (checkouts["writer-a"] / "hosting-plan-smoke.txt").write_text("selected lifecycle payload\n", encoding="utf-8")
                checkpoint_a = writer_a.publish_snapshot("checkpoint", ["plans/P-SMOKE.md", "hosting-plan-smoke.txt"],
                                                         summary="Plan ready at S-001", state=state_record())
                if checkpoint_a["ownership"] != "owned" or checkpoint_a["code_publication"] != "published":
                    raise ValueError("Writer A checkpoint must retain ownership.")
                checkpoint_a_revision = checkpoint_a["published_revision"]
                verify_parent(writer_a.backend, checkpoint_a_revision, acquire_a_revision)
                records = writer_a.backend.records(checkpoint_a_revision)
                assert_owner(records, session_a)
                if records["state"] != dict(state_record(), summary="Plan ready at S-001"):
                    raise ValueError("Checkpoint must publish the exact working Plan cursor.")
                if writer_a.backend.run("show", checkpoint_a_revision + ":plans/P-SMOKE.md").stdout != plan_text().encode():
                    raise ValueError("Checkpoint must publish the selected Plan text.")
                released_a = writer_a.release()
                if released_a["ownership"] != "released":
                    raise ValueError("Writer A release must clear ownership.")
                release_a_revision = released_a["published_revision"]
                verify_parent(writer_a.backend, release_a_revision, checkpoint_a_revision)
                records = writer_a.backend.records(release_a_revision)
                if records["owner"]["owner"] is not None or records["state"] != dict(state_record(), summary="Plan ready at S-001"):
                    raise ValueError("Release must preserve the committed Plan and cursor.")
                if writer_a.backend.run("show", release_a_revision + ":plans/P-SMOKE.md").stdout != plan_text().encode():
                    raise ValueError("Release must preserve the exact Plan text.")
                final_revision = release_a_revision
                reacquired_a = writer_a.acquire()
                if reacquired_a["ownership"] != "owned":
                    raise ValueError("Writer A must reacquire before takeover test.")
                reacquire_a_revision = reacquired_a["published_revision"]
                verify_parent(writer_a.backend, reacquire_a_revision, release_a_revision)
                session_a = writer_a.sessions.prove(writer_a.backend, reacquire_a_revision,
                                                    writer_a.backend.records(reacquire_a_revision))
                assert_owner(writer_a.backend.records(reacquire_a_revision), session_a)
                selected = checkouts["writer-a"] / "hosting-plan-smoke.txt"
                selected.write_text("old writer retained edit\n", encoding="utf-8")
                git("-C", str(checkouts["writer-a"]), "add", "--", "hosting-plan-smoke.txt")
                saved_head = git("-C", str(checkouts["writer-a"]), "rev-parse", "HEAD")
                saved_index = (checkouts["writer-a"] / ".git/index").read_bytes()
                with Runtime(checkouts["writer-b"], private["writer-b"], authority, contexts["writer-b"]) as writer_b:
                    initialized_b = writer_b.initialize()
                    if initialized_b["observed_revision"] != reacquire_a_revision:
                        raise ValueError("Writer B must bind to Writer A's exact current revision.")
                    proposal = writer_b.acquire()
                    if proposal["status"] != "decision_required" or proposal["decision"]["category"] != "takeover":
                        raise ValueError("Writer B must receive an explicit takeover decision.")
                    taken = writer_b.apply_decision(proposal["decision"]["decision_id"], "takeover")
                    if taken["ownership"] != "owned":
                        raise ValueError("Writer B takeover must acquire ownership.")
                    takeover_revision = taken["published_revision"]
                    verify_parent(writer_b.backend, takeover_revision, reacquire_a_revision)
                    session_b = writer_b.sessions.prove(writer_b.backend, takeover_revision,
                                                        writer_b.backend.records(takeover_revision))
                    if session_b["session_id"] == session_a["session_id"]:
                        raise ValueError("Takeover must create a new session identity.")
                    takeover_new_session = True
                    assert_owner(writer_b.backend.records(takeover_revision), session_b)
                    final_revision = takeover_revision
                    fence_head = observer.remote_head()
                    failures = []
                    for label, operation in (("checkpoint", lambda: writer_a.publish_snapshot(
                            "checkpoint", ["hosting-plan-smoke.txt"], summary="Must remain local")),
                            ("release", writer_a.release)):
                        try:
                            operation()
                        except RuntimeFailure as error:
                            if error.code != 4:
                                raise ValueError("Old writer operation failed outside the fenced state.") from None
                            failures.append(label)
                        else:
                            raise ValueError("Fenced writer operation unexpectedly succeeded.")
                        if (observer.remote_head() != fence_head or selected.read_text(encoding="utf-8") != "old writer retained edit\n" or
                                (checkouts["writer-a"] / ".git/index").read_bytes() != saved_index or
                                git("-C", str(checkouts["writer-a"]), "rev-parse", "HEAD") != saved_head):
                            raise ValueError("Fenced operation changed remote or local Git state.")
                    if failures != ["checkpoint", "release"]:
                        raise ValueError("Both old writer mutations must be fenced.")

                    # Writer B materializes only the explicit smoke fixture in its independent checkout.
                    plan_dir = checkouts["writer-b"] / "plans"
                    plan_dir.mkdir(parents=True, exist_ok=True)
                    (plan_dir / "P-SMOKE.md").write_text(plan_text(), encoding="utf-8")
                    payload = checkouts["writer-b"] / "hosting-plan-smoke.txt"
                    payload.write_text("writer B checkpoint payload\n", encoding="utf-8")
                    checkpoint_b = writer_b.publish_snapshot("checkpoint", ["plans/P-SMOKE.md", "hosting-plan-smoke.txt"],
                                                             summary="Writer B checkpoint at S-001")
                    checkpoint_b_revision = checkpoint_b["published_revision"]
                    verify_parent(writer_b.backend, checkpoint_b_revision, takeover_revision)
                    records = writer_b.backend.records(checkpoint_b_revision)
                    assert_owner(records, session_b)
                    expected_working = dict(state_record(), summary="Writer B checkpoint at S-001")
                    if (records["state"] != expected_working or
                            writer_b.backend.run("show", checkpoint_b_revision + ":plans/P-SMOKE.md").stdout != plan_text().encode() or
                            writer_b.backend.text("show", checkpoint_b_revision + ":hosting-plan-smoke.txt") != "writer B checkpoint payload"):
                        raise ValueError("Writer B checkpoint must preserve the working Plan and cursor.")
                    payload.write_text("writer B handoff payload\n", encoding="utf-8")
                    handoff = writer_b.publish_snapshot("handoff", ["plans/P-SMOKE.md", "hosting-plan-smoke.txt"],
                                                        summary="Handoff at S-001")
                    handoff_revision = handoff["published_revision"]
                    verify_parent(writer_b.backend, handoff_revision, checkpoint_b_revision)
                    records = writer_b.backend.records(handoff_revision)
                    if records["owner"]["owner"] is not None or records["state"] != dict(state_record(), summary="Handoff at S-001"):
                        raise ValueError("Handoff must clear owner and preserve the Plan cursor.")
                    if (writer_b.backend.run("show", handoff_revision + ":plans/P-SMOKE.md").stdout != plan_text().encode() or
                            writer_b.backend.text("show", handoff_revision + ":hosting-plan-smoke.txt") != "writer B handoff payload"):
                        raise ValueError("Handoff must publish the exact selected snapshot.")
                    final_revision = handoff_revision
                    reacquired_b = writer_b.acquire()
                    if reacquired_b["ownership"] != "owned":
                        raise ValueError("Writer B must reacquire before done.")
                    reacquire_b_revision = reacquired_b["published_revision"]
                    verify_parent(writer_b.backend, reacquire_b_revision, handoff_revision)
                    session_b = writer_b.sessions.prove(writer_b.backend, reacquire_b_revision,
                                                        writer_b.backend.records(reacquire_b_revision))
                    assert_owner(writer_b.backend.records(reacquire_b_revision), session_b)
                    done = writer_b.publish_snapshot("done", [], summary="P-SMOKE completed", release_intent=False)
                    done_revision = done["published_revision"]
                    verify_parent(writer_b.backend, done_revision, reacquire_b_revision)
                    records = writer_b.backend.records(done_revision)
                    assert_owner(records, session_b)
                    if records["state"] != {"schema_version": 1, "status": "idle", "active_plan": None,
                                             "current_step": None, "blocker": None, "summary": "P-SMOKE completed"}:
                        raise ValueError("Done must reset state and preserve ownership when release is false.")
                    done_plan = writer_b.backend.text("show", done_revision + ":plans/P-SMOKE.md")
                    if parse_plan(done_plan + "\n")["status"] != "DONE":
                        raise ValueError("Done must complete the active Plan.")
                    final_revision = done_revision
                    released_b = writer_b.release()
                    final_revision = released_b["published_revision"]
                    verify_parent(writer_b.backend, final_revision, done_revision)
                    if writer_b.backend.records(final_revision)["owner"]["owner"] is not None:
                        raise ValueError("Final release must clear ownership.")
            if mainline_backend.remote_head() != mainline_before or mainline_before != base:
                raise ValueError("Mainline changed during Plan lifecycle acceptance.")
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
                    "mainline_unchanged": True, "mainline_revision": base, "git_version": git("--version"),
                    "python_version": platform.python_version(), "provider": "git-remote", "provider_version": None,
                    "initial_revision": init_revision, "writer_a_checkpoint": checkpoint_a_revision,
                    "writer_a_release": release_a_revision, "writer_a_reacquire": reacquire_a_revision,
                    "takeover_revision": takeover_revision, "writer_b_checkpoint": checkpoint_b_revision,
                    "handoff_revision": handoff_revision, "writer_b_reacquire": reacquire_b_revision,
                    "done_revision": done_revision, "final_revision": final_revision,
                    "writer_a_status": checkpoint_a["status"], "takeover_status": taken["status"],
                    "checkpoint_status": checkpoint_b["status"], "handoff_status": handoff["status"],
                    "done_status": done["status"], "fenced_operations": failures,
                    "plan_release_preserved": True, "takeover_fenced_old_writer": True,
                    "takeover_new_session": takeover_new_session,
                    "handoff_preserved_working_cursor": True, "done_completed_plan": True,
                    "done_retained_owner": True}
    except Exception:
        # Preserve the isolated branch unless the expected-final-OID lease deletion succeeded.
        return {"passed": False, "publication_ref": ref, "branch_deleted": branch_deleted,
                "mainline_unchanged": False, "error": "Hosting Plan smoke failed; private backend details were withheld."}


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
