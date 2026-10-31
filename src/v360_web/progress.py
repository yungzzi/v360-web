"""Per-worker progress hook; values contain no device credentials."""

from contextvars import ContextVar

from .errors import CameraError

callback = ContextVar("v360_progress", default=None)
shutdown_event = ContextVar("v360_shutdown", default=None)
cancel_event = ContextVar("v360_cancel", default=None)


def check_cancelled():
    shutdown = shutdown_event.get()
    if shutdown is not None and shutdown.is_set():
        raise CameraError("interrupted", "Service shutdown interrupted this operation.")
    event = cancel_event.get()
    if event is not None and event.is_set():
        raise CameraError("backup_yielded", "Background backup paused for another operation.")


def report(stage, **values):
    check_cancelled()
    target = callback.get()
    if target:
        target({"stage": stage, **values})
