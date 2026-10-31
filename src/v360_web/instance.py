"""Cross-process ownership of a local data directory; held throughout service life."""

import fcntl
import os
from pathlib import Path

from .errors import CameraError


class InstanceLock:
    def __init__(self, root: Path):
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = root / ".service.lock"
        self.file = None

    def __enter__(self):
        if self.path.is_symlink():
            raise CameraError("instance_path", "Service lock must be a regular local file.")
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        self.file = os.fdopen(fd, "r+")
        try:
            fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            self.file.close()
            self.file = None
            raise CameraError(
                "instance_running", "Another service owns this data directory."
            ) from error
        return self

    def __exit__(self, *args):
        self.file.close()
        self.file = None
