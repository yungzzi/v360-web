"""Download pinned official Android tools into this project; no global installation."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / ".tools"
REPOSITORY = "https://dl.google.com/android/repository/"


def fetch(url: str, target: Path) -> None:
    if target.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".part")
    print("Downloading", url.rsplit("/", 1)[-1], flush=True)
    with urllib.request.urlopen(url, timeout=90) as source, temporary.open("wb") as dest:
        shutil.copyfileobj(source, dest)
    temporary.replace(target)


def main() -> None:
    index = TOOLS / "downloads/android-repository.xml"
    fetch(REPOSITORY + "repository2-3.xml", index)
    tree = ET.parse(index)
    packages = {p.attrib["path"]: p for p in tree.getroot().findall("remotePackage")}
    manifest = []
    windows = "--windows-adb" in sys.argv
    names = (
        ["platform-tools"]
        if windows
        else ["platforms;android-34", "build-tools;35.0.0", "platform-tools"]
    )
    for name in names:
        package = packages[name]
        choices = package.findall("archives/archive")
        archive = next(
            a
            for a in choices
            if a.findtext("host-os", "linux") == ("windows" if windows else "linux")
        )
        complete = archive.find("complete")
        url = REPOSITORY + complete.findtext("url")
        checksum = complete.find("checksum")
        algorithm = checksum.attrib.get("type", "sha1")
        target = TOOLS / "downloads" / url.rsplit("/", 1)[-1]
        fetch(url, target)
        actual = hashlib.new(algorithm, target.read_bytes()).hexdigest()
        if actual.lower() != checksum.text.strip().lower():
            raise RuntimeError(f"Official repository checksum mismatch: {name}")
        destination = (
            TOOLS / "android-windows" if windows else TOOLS / "android" / name.replace(";", "-")
        )
        if not destination.exists():
            destination.mkdir(parents=True)
            with zipfile.ZipFile(target) as z:
                for entry in z.infolist():
                    path = destination / entry.filename
                    if not path.resolve().is_relative_to(destination.resolve()):
                        raise RuntimeError("Archive member escapes tool directory")
                    z.extract(entry, destination)
                    if not entry.is_dir():
                        path.chmod(((entry.external_attr >> 16) & 0o777) or 0o644)
        manifest.append(
            {
                "package": name,
                "url": url,
                "checksum_algorithm": algorithm,
                "verified_checksum": actual,
                "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            }
        )
        print("Verified", name, flush=True)
    provenance_name = (
        "android-windows-provenance.json" if windows else "android-tools-provenance.json"
    )
    (TOOLS / provenance_name).write_text(json.dumps(manifest, indent=2))
    java_root = TOOLS / "sysroot/usr/lib/jvm/java-17-openjdk-amd64"
    # Debian relocates Java configuration through /etc symlinks. Keep it project-local.
    for p in java_root.rglob("*"):
        if p.is_symlink():
            link = os.readlink(p)
            if link.startswith("/etc/java-"):
                replacement = TOOLS / "sysroot" / link.lstrip("/")
                p.unlink()
                p.symlink_to(os.path.relpath(replacement, p.parent))
    print("Android build tools ready", flush=True)


if __name__ == "__main__":
    main()
