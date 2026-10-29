"""Extract Ubuntu's verified FFmpeg packages locally without sudo or installation."""

import hashlib
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / ".tools"
downloads = TOOLS / "downloads"
downloads.mkdir(parents=True, exist_ok=True)
plan = subprocess.check_output(
    ["apt-get", "-s", "--no-install-recommends", "install", "ffmpeg"], text=True
)
names = sorted(set(re.findall(r"^Inst (\S+)", plan, re.MULTILINE)) | {"ffmpeg"})
print("Downloading media dependencies:", len(names), flush=True)
subprocess.run(["apt-get", "download", *names], cwd=downloads, check=True)
provenance = []
for package in downloads.glob("*.deb"):
    name = subprocess.check_output(["dpkg-deb", "-f", str(package), "Package"], text=True).strip()
    if name not in names:
        continue
    version = subprocess.check_output(
        ["dpkg-deb", "-f", str(package), "Version"], text=True
    ).strip()
    subprocess.run(["dpkg-deb", "-x", str(package), str(TOOLS / "media-root")], check=True)
    provenance.append(
        {
            "package": name,
            "version": version,
            "sha256": hashlib.sha256(package.read_bytes()).hexdigest(),
        }
    )
(TOOLS / "media-provenance.json").write_text(json.dumps(provenance, indent=2))
binary_dir = TOOLS / "bin"
binary_dir.mkdir(exist_ok=True)
for name in ["ffmpeg", "ffprobe"]:
    wrapper = binary_dir / name
    wrapper.write_text(
        "#!/usr/bin/env python3\nimport os\nfrom pathlib import Path\n"
        "root=Path(__file__).resolve().parents[1]/'media-root'\n"
        "env=dict(os.environ)\n"
        "paths={str(p.parent) for p in (root/'usr/lib').rglob('*.so*')}\n"
        "env['LD_LIBRARY_PATH']=':'.join(sorted(paths))\n"
        f"program=root/'usr/bin/{name}'\n"
        "os.execve(str(program),[str(program),*__import__('sys').argv[1:]],env)\n"
    )
    wrapper.chmod(0o755)
print("Project-local media tools ready", flush=True)
