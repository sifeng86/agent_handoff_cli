"""Exact revision materialization into a new disposable private worktree."""
from pathlib import Path

from .contracts import RuntimeFailure
from .journal import opaque_id


def materialize(backend, store, session: dict, oid: str) -> Path:
    parent = store.root / "checkouts"
    if parent.is_symlink():
        raise RuntimeFailure("Managed checkout parent is unsafe.")
    parent.mkdir(mode=0o700, exist_ok=True)
    destination = parent / session["session_id"]
    if destination.exists() or destination.is_symlink():
        destination = parent / (session["session_id"] + "-" + opaque_id())
    backend.text("worktree", "add", "--quiet", "--detach", str(destination), oid)
    # Each worktree has its own HEAD; verify through that checkout rather than the bare cache HEAD.
    from .preservation import checkout_git
    if checkout_git(destination, "rev-parse", "HEAD").decode().strip() != oid:
        raise RuntimeFailure("Managed checkout revision does not match the committed snapshot.", "invalid", 7)
    return destination
