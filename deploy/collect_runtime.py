"""Collect shared-library closure of Debian-provided CLI tools, excluding unused emulators/drivers."""

import shutil
import subprocess
from pathlib import Path

root = Path("/opt/runtime-root")
root.mkdir()
paths = {Path("/usr/bin") / name for name in ("ffmpeg", "ffprobe", "qemu-aarch64")}
for tool in list(paths):
    result = subprocess.check_output(["ldd", str(tool)], text=True)
    if "not found" in result:
        raise RuntimeError("Missing build-stage runtime dependency")
    for line in result.splitlines():
        value = line.split("=>", 1)[-1].strip().split()[0]
        if value.startswith("/"):
            paths.add(Path(value))
for source in paths:
    target = root / str(source.parent.resolve() / source.name).lstrip("/")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target, follow_symlinks=True)
for source in Path("/usr/share/doc").glob("*/copyright"):
    target = root / "usr/share/v360-web/licenses" / source.parent.name / "copyright"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target, follow_symlinks=True)
manifest = root / "usr/share/v360-web"
manifest.mkdir(parents=True, exist_ok=True)
(manifest / "debian-packages.txt").write_text(
    subprocess.check_output(["dpkg-query", "-W"], text=True)
)
(manifest / "runtime-files.txt").write_text("\n".join(sorted(str(p) for p in paths)) + "\n")
