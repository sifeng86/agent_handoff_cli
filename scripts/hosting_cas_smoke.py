"""Conditional publication smoke; leaves an isolated branch as evidence."""
import argparse
import json
import multiprocessing
from pathlib import Path
import re
import sys
import tempfile
import uuid


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_handoff.git_backend import GitBackend


def writer(directory, destination, ref, expected, label, ready, start, outcomes):
    try:
        backend = GitBackend(Path(directory), destination, ref)
        if backend.observe() != expected:
            raise ValueError("Observation changed before preparation.")
        candidate, _ = backend.prepare(expected, {"agent-handoff-smoke.txt": label.encode()},
                                       "Agent Handoff isolated hosting CAS smoke\n")
        backend.pin(uuid.uuid4().hex, candidate)
        ready.put(label)
        if not start.wait(60):
            raise ValueError("Preparation barrier timed out.")
        outcomes.put({"writer": label, "candidate": candidate, "outcome": backend.push(candidate, expected)})
    except Exception:
        outcomes.put({"writer": label, "outcome": "smoke_error"})


def run(destination, mainline, base):
    if not re.fullmatch(r"[0-9a-f]{40}", base):
        raise ValueError("Explicit SHA-1 base is required.")
    ref = "refs/heads/agent-handoff-smoke-" + uuid.uuid4().hex
    with tempfile.TemporaryDirectory(prefix="agent-handoff-hosting-") as temporary:
        root = Path(temporary)
        main = GitBackend(root / "main.git", destination, mainline)
        if main.remote_head() != base:
            raise ValueError("Explicit base differs from current mainline.")
        backend = GitBackend(root / "control.git", destination, ref)
        if backend.remote_head() is not None:
            raise ValueError("Smoke ref must be absent.")
        backend.text("fetch", "--quiet", "--no-tags", "--", destination, base)
        initial, _ = backend.prepare(base, {"agent-handoff-smoke.txt": b"isolated CAS smoke\n"},
                                     "Agent Handoff isolated hosting smoke initialization\n")
        backend.pin(uuid.uuid4().hex, initial)
        if backend.push(initial, None) != "published":
            raise ValueError("Expected-absent creation was not confirmed.")
        context = multiprocessing.get_context("spawn")
        ready, outcomes, start = context.Queue(), context.Queue(), context.Event()
        workers = [context.Process(target=writer, args=(str(root / (label + ".git")), destination,
                   ref, initial, label, ready, start, outcomes)) for label in ("writer-a", "writer-b")]
        try:
            for process in workers:
                process.start()
            labels = {ready.get(timeout=60), ready.get(timeout=60)}
            if labels != {"writer-a", "writer-b"}:
                raise ValueError("Both writers must prepare before sending.")
            start.set()
            reports = [outcomes.get(timeout=60), outcomes.get(timeout=60)]
            for process in workers:
                process.join(timeout=5)
            current = backend.observe()
            passed = (sorted(item["outcome"] for item in reports) == ["published", "rejected"] and
                      current == next(item["candidate"] for item in reports if item["outcome"] == "published") and
                      backend.text("rev-list", "--parents", "-n", "1", current).split() == [current, initial] and
                      main.remote_head() == base)
            return {"passed": passed, "publication_ref": ref, "initial_revision": initial,
                    "observed_revision": current, "writers": sorted(reports, key=lambda item: item["writer"]),
                    "mainline_unchanged": main.remote_head() == base, "test_branch_retained": True}
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
        report = {"passed": False, "error": "Hosting smoke failed; no raw backend response is exposed."}
    print(json.dumps(report))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
