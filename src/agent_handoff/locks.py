"""Kernel-backed nonblocking locks. No timestamp-based stale detection."""
from contextlib import contextmanager, ExitStack
import fcntl
import os

from .contracts import RuntimeFailure
from .paths import PrivateStore


@contextmanager
def lock(store: PrivateStore, name: str):
    fd = store.open_file(name, create=True)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeFailure("Another runtime command holds the mutation lock.", "rejected", 4) from None
        yield
    finally:
        os.close(fd)


@contextmanager
def mutation_locks(store: PrivateStore, session_id: str | None, checkout_id: str):
    with ExitStack() as stack:
        stack.enter_context(lock(store, "repository.lock"))
        if session_id is not None:
            stack.enter_context(lock(store, "session-" + session_id + ".lock"))
        stack.enter_context(lock(store, "checkout-" + checkout_id + ".lock"))
        yield
