"""Live ownership race and crash-recovery acceptance on an isolated branch."""
import argparse
import json
import multiprocessing
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import uuid
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_handoff.authority import Authority
from agent_handoff.git_backend import GitBackend, git_environment
from agent_handoff.publication import Runtime
from agent_handoff.contracts import RuntimeFailure


def acquire_worker(checkout, private, destination, ref, context_id, label, ready, start, outcomes):
    def barrier(point):
        if point == "before_remote_send":
            ready.put(label)
            if not start.wait(60):
                raise ValueError("Acquisition barrier timed out.")
    try:
        with Runtime(Path(checkout), Path(private), Authority(destination, ref), context_id, fault=barrier) as runtime:
            runtime.initialize()
            report = runtime.acquire()
            outcomes.put({"writer": label, "code_publication": report["code_publication"],
                          "ownership": report["ownership"]})
    except Exception:
        outcomes.put({"writer": label, "code_publication": "smoke_error"})


def crash_worker(checkout, private, destination, ref, context_id, action="acquire"):
    def crash(point):
        if point == "after_server_accept_before_client_reply":
            os._exit(77)
    with Runtime(Path(checkout), Path(private), Authority(destination, ref), context_id, fault=crash) as runtime:
        if action == "acquire":
            runtime.acquire()
        elif action == "release":
            runtime.release()
        else:
            raise ValueError("Unsupported crash action.")


def run(destination, mainline, base):
    if not re.fullmatch(r"[0-9a-f]{40}", base):
        raise ValueError("Explicit SHA-1 base is required.")
    if not mainline.startswith("refs/heads/"):
        raise ValueError("Explicit full mainline ref is required.")
    ref = "refs/heads/agent-handoff-runtime-smoke-" + uuid.uuid4().hex
    authority = Authority(destination, ref)
    context = multiprocessing.get_context("spawn")
    with tempfile.TemporaryDirectory(prefix="agent-handoff-runtime-hosting-") as temporary:
        root = Path(temporary)
        mainline_backend = GitBackend(root / "main.git", destination, mainline)
        if mainline_backend.remote_head() != base:
            raise ValueError("Mainline must match the explicit base.")
        checkouts = {}
        for label in ("writer-a", "writer-b", "crash"):
            path = root / label
            command = ["git", "-c", "core.hooksPath=/dev/null", "clone", "--quiet", "--no-checkout",
                       "--branch", mainline[len("refs/heads/"):], "--", destination, str(path)]
            clone = subprocess.run(command, env=git_environment(), capture_output=True, timeout=30, umask=0o077)
            if clone.returncode:
                raise ValueError("Isolated clone failed.")
            checkouts[label] = path
        contexts = {label: uuid.uuid4().hex + uuid.uuid4().hex for label in checkouts}
        with Runtime(checkouts["writer-a"], root / "private-init", authority) as runtime:
            initial_report = runtime.initialize(base, mainline_ref=mainline)
            if initial_report["code_publication"] != "published":
                raise ValueError("Initialization must create the expected-absent ref.")
            initial = runtime.backend.observe()
        ready, outcomes, start = context.Queue(), context.Queue(), context.Event()
        workers = [context.Process(target=acquire_worker, args=(str(checkouts[label]), str(root / ("private-" + label)),
                   destination, ref, contexts[label], label, ready, start, outcomes)) for label in ("writer-a", "writer-b")]
        try:
            for process in workers:
                process.start()
            if {ready.get(timeout=60), ready.get(timeout=60)} != {"writer-a", "writer-b"}:
                raise ValueError("Both acquisitions must prepare before sending.")
            start.set()
            reports = [outcomes.get(timeout=60), outcomes.get(timeout=60)]
            if sorted(item["code_publication"] for item in reports) != ["published", "rejected"]:
                raise ValueError("Exactly one acquisition must publish.")
            winner = next(item["writer"] for item in reports if item["code_publication"] == "published")
            with Runtime(checkouts[winner], root / ("private-" + winner), authority, contexts[winner]) as runtime:
                acquisition = runtime.backend.observe()
                if runtime.backend.text("rev-list", "--parents", "-n", "1", acquisition).split() != [acquisition, initial]:
                    raise ValueError("Acquisition must have the exact observed parent.")
                if runtime.recover()["ownership"] != "owned" or runtime.release()["ownership"] != "released":
                    raise ValueError("Winning capability must recover and release.")
                released = runtime.backend.observe()
                if runtime.backend.text("diff", "--name-only", acquisition, released).splitlines() != [".agent/owner.json"]:
                    raise ValueError("Release must change only the owner record.")
            private = root / "private-crash"
            with Runtime(checkouts["crash"], private, authority, contexts["crash"]) as runtime:
                runtime.initialize()
            process = context.Process(target=crash_worker, args=(str(checkouts["crash"]), str(private), destination, ref, contexts["crash"]))
            process.start()
            try:
                process.join(timeout=60)
                if process.exitcode != 77:
                    raise ValueError("Crash must occur after confirmed server acceptance.")
                with Runtime(checkouts["crash"], private, authority, contexts["crash"]) as restarted:
                    pending = restarted.journal.pending()
                    if len(pending) != 1:
                        raise ValueError("Exactly one durable pending operation is required.")
                    accepted = restarted.backend.observe()
                    resolved = restarted.resolve(pending[0]["operation_id"])
                    recovered = restarted.recover()
                    if (resolved["code_publication"] != "published" or recovered["ownership"] != "owned" or
                            restarted.backend.observe() != accepted or restarted.journal.pending()):
                        raise ValueError("Recovery must prove publication without replay.")
                    selected = checkouts["crash"] / "runtime-smoke.txt"
                    selected.write_text("frozen-hosting-snapshot\n")
                    source_head = subprocess.check_output(["git", "-C", str(checkouts["crash"]), "rev-parse", "HEAD"],
                                                          env=git_environment(), stderr=subprocess.PIPE)
                    index_path = checkouts["crash"] / ".git/index"
                    source_index = index_path.read_bytes() if index_path.exists() else None
                    def editor(point):
                        if point == "before_remote_send":
                            selected.write_text("editor-change-retained\n")
                    restarted.fault = editor
                    try:
                        snapshot = restarted.publish_snapshot("checkpoint", ["runtime-smoke.txt"], summary="Fixed hosting snapshot")
                    finally:
                        restarted.fault = lambda point: None
                    snapshot_revision = snapshot["published_revision"]
                    if (snapshot["code_publication"] != "published" or snapshot["ownership"] != "owned" or
                            restarted.backend.text("show", snapshot_revision + ":runtime-smoke.txt") != "frozen-hosting-snapshot" or
                            selected.read_text() != "editor-change-retained\n" or
                            (index_path.read_bytes() if index_path.exists() else None) != source_index or
                            subprocess.check_output(["git", "-C", str(checkouts["crash"]), "rev-parse", "HEAD"],
                                                    env=git_environment(), stderr=subprocess.PIPE) != source_head):
                        raise ValueError("Published snapshot must retain subsequent editor/index/head state locally.")
                    session_id = restarted.sessions.current()["session_id"]
                    release_process = context.Process(target=crash_worker,
                        args=(str(checkouts["crash"]), str(private), destination, ref, contexts["crash"], "release"))
                    release_process.start()
                    try:
                        release_process.join(timeout=60)
                        if release_process.exitcode != 77:
                            raise ValueError("Release must crash after the publication boundary.")
                        release_pending = restarted.journal.pending()
                        if len(release_pending) != 1 or release_pending[0]["operation_type"] != "release":
                            raise ValueError("Exactly one release intent must survive.")
                        final = restarted.backend.observe()
                        with patch.object(restarted.backend, "push", side_effect=ValueError("No replay permitted")) as push:
                            try:
                                restarted.recover()
                            except RuntimeFailure as error:
                                if error.code != 5:
                                    raise
                            else:
                                raise ValueError("Pending release must block writer recovery.")
                            release_resolved = restarted.resolve(release_pending[0]["operation_id"])
                            if (release_resolved["code_publication"] != "published" or
                                    release_resolved["ownership"] != "released" or
                                    restarted.resolve(release_pending[0]["operation_id"])["ownership"] != "released"):
                                raise ValueError("Release recovery must prove the exact publication.")
                            push.assert_not_called()
                        if (restarted.backend.observe() != final or restarted.journal.pending() or
                                not restarted.store.read("session-" + session_id + ".json")["revoked"]):
                            raise ValueError("Release recovery must not revive the writer or publish again.")
                    finally:
                        if release_process.is_alive():
                            release_process.terminate()
                        release_process.join(timeout=5)
                    if restarted.backend.text("diff", "--name-only", snapshot_revision, final).splitlines() != [".agent/owner.json"]:
                        raise ValueError("Recovered release must change only the owner record.")
            finally:
                if process.is_alive():
                    process.terminate()
                process.join(timeout=5)
            unchanged = mainline_backend.remote_head() == base
            return {"passed": unchanged, "publication_ref": ref, "initial_revision": initial,
                    "acquisition_revision": acquisition, "writers": sorted(reports, key=lambda item: item["writer"]),
                    "A02": True, "A04": True, "A09": True, "A15": True, "A16": True, "A20": True,
                    "crash_accepted_revision": accepted, "release_crash_accepted_revision": final,
                    "snapshot_revision": snapshot_revision, "snapshot_editor_changes_retained": True,
                    "recovery_replayed": False, "mainline_unchanged": unchanged}
        finally:
            start.set()
            for process in workers:
                if process.pid is not None:
                    if process.is_alive():
                        process.terminate()
                    process.join(timeout=5)
            for channel in (ready, outcomes):
                channel.close()
                channel.join_thread()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--authority", required=True)
    parser.add_argument("--mainline-ref", required=True)
    parser.add_argument("--base", required=True)
    args = parser.parse_args()
    try:
        report = run(args.authority, args.mainline_ref, args.base)
    except Exception:
        report = {"passed": False, "error": "Hosting runtime smoke failed; backend responses are private."}
    print(json.dumps(report))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
