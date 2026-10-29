"""Per-worker progress hook; values contain no device credentials."""

from contextvars import ContextVar

callback = ContextVar("v360_progress", default=None)


def report(stage, **values):
    target = callback.get()
    if target:
        target({"stage": stage, **values})
