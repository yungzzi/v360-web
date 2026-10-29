"""Launch the project-local ARM64 worker with no secrets in argv or a network listener."""

import os
import resource
from pathlib import Path

root = Path(__file__).resolve().parents[1]
runtime = root / ".private/android-runtime-arm64"
qemu = root / ".tools/phone-free/sysroot/usr/bin/qemu-aarch64"
worker = root / ".tools/phone-free/build/native-worker-probe"
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
os.execv(
    str(qemu),
    [
        str(qemu),
        "-L",
        str(runtime),
        "-E",
        "LD_LIBRARY_PATH=" + str(runtime) + ":" + str(root / ".private/android-libs/arm64-v8a"),
        str(worker),
    ],
)
