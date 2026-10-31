"""Launch the project-local ARM64 worker with no secrets in argv or a network listener."""

import os
import resource
from pathlib import Path

root = Path(__file__).resolve().parents[1]
runtime_root = Path(os.environ.get("V360_RUNTIME_ROOT", str(root / ".private")))
runtime = runtime_root / "android-runtime-arm64"
qemu = Path(
    os.environ.get("V360_QEMU", str(root / ".tools/phone-free/sysroot/usr/bin/qemu-aarch64"))
)
worker = Path(
    os.environ.get("V360_WORKER_PATH", str(root / ".tools/phone-free/build/native-worker-probe"))
)
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
os.execv(
    str(qemu),
    [
        str(qemu),
        "-L",
        str(runtime),
        "-E",
        "LD_LIBRARY_PATH=" + str(runtime) + ":" + str(runtime_root / "android-libs/arm64-v8a"),
        str(worker),
    ],
)
