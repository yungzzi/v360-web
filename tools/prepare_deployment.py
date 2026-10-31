"""Create a new private deployment directory. Never copy recordings or overwrite targets."""

import argparse
import hashlib
import json
import os
import shutil
from pathlib import Path

from v360_web.backup_policy import BackupPolicy
from v360_web.config import read_private_json, write_private_json


def prepare(project, destination, config):
    project, destination, config = project.resolve(), destination.resolve(), config.resolve()
    if destination.exists():
        raise ValueError("Destination already exists; choose a new directory.")
    os.umask(0o077)
    destination.mkdir(parents=True, mode=0o700)
    for folder in ("config", "runtime", "data", "backups"):
        (destination / folder).mkdir(mode=0o700)
    private = project / ".private"
    cfg = read_private_json(config)
    profile = (config.parent / cfg.get("sdk_profiles", "sdk-profiles.json")).resolve()
    cfg.update(data_root="../data", sdk_profiles="sdk-profiles.json")
    cfg["bridge"] = {"adapter": "native-arm64", "host": "127.0.0.1", "port": 8766}
    for name, value in (
        ("device.json", cfg),
        ("sdk-profiles.json", read_private_json(profile)),
        ("web.json", read_private_json(config.parent / "web.json")),
    ):
        write_private_json(destination / "config" / name, value)
    runtime = destination / "runtime"
    android = runtime / "android-runtime-arm64"
    android.mkdir(mode=0o700)
    for name in (
        "linker64",
        "libc.so",
        "libm.so",
        "libdl.so",
        "libstdc++.so",
        "libc++.so",
        "liblog.so",
    ):
        shutil.copyfile(private / "android-runtime-arm64" / name, android / name)
        (android / name).chmod(0o700 if name == "linker64" else 0o600)
    linker = android / "system/bin"
    linker.mkdir(parents=True, mode=0o700)
    (linker / "linker64").symlink_to("../../linker64")
    libraries = runtime / "android-libs/arm64-v8a"
    libraries.mkdir(parents=True, mode=0o700)
    shutil.copyfile(private / "android-libs/arm64-v8a/libPPCS_API.so", libraries / "libPPCS_API.so")
    (libraries / "libPPCS_API.so").chmod(0o600)
    shutil.copyfile(
        project / ".tools/phone-free/build/native-worker-probe", runtime / "native-worker"
    )
    (runtime / "native-worker").chmod(0o700)
    hashes = {
        str(p.relative_to(runtime)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in runtime.rglob("*")
        if p.is_file() and not p.is_symlink()
    }
    write_private_json(runtime / "manifest.json", {"sha256": hashes})
    policy = BackupPolicy(destination / "data")
    policy.write(
        "policy", {"enabled": False, "mode": "local", "interval_minutes": 60, "retention_days": 30}
    )
    shutil.copyfile(project / "compose.yaml", destination / "compose.yaml")
    shutil.copyfile(project / "compose.ops.yaml", destination / "compose.ops.yaml")
    (destination / "tools").mkdir(mode=0o700)
    shutil.copyfile(project / "tools/release.py", destination / "tools/release.py")
    (destination / ".env").write_text(
        "V360_IMAGE=v360-web:deployment-dev\nV360_HOME="
        + str(destination)
        + "\nV360_PORT=18089\nV360_UID="
        + str(os.getuid())
        + "\nV360_GID="
        + str(os.getgid())
        + "\nV360_SECURE_COOKIES=0\n"
    )
    return {
        "prepared": True,
        "automatic_backup": "paused",
        "credentials": "not_printed",
        "camera_connection": "not_attempted",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--config", type=Path, default=Path(".private/device.json"))
    args = parser.parse_args()
    print(json.dumps(prepare(Path(__file__).resolve().parents[1], args.destination, args.config)))


if __name__ == "__main__":
    main()
