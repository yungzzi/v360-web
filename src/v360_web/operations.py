"""Consistent metadata backups and offline restore. Never delete camera recordings."""

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from . import storage
from .config import read_private_json, write_private_json
from .errors import CameraError
from .instance import InstanceLock

CONFIG_FILES = ("device.json", "sdk-profiles.json", "web.json")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_database(path):
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("Database integrity check failed")
        return db.execute("SELECT count(*) FROM recordings").fetchone()[0]


def verify(folder):
    info = read_private_json(folder / "manifest.json")
    if info.get("format") != 1 or set(info["sha256"]) != {"recordings.sqlite3", *CONFIG_FILES}:
        raise ValueError("Unsupported backup manifest")
    for name, sha in info["sha256"].items():
        path = folder / name
        if path.is_symlink() or digest(path) != sha:
            raise ValueError("Backup hash mismatch")
    count = check_database(folder / "recordings.sqlite3")
    for name in CONFIG_FILES:
        read_private_json(folder / name)
    return {"verified": True, "records": count, "recording_bytes_included": False}


def backup(data, config, backups):
    data, config, backups = data.resolve(), config.resolve(), backups.resolve()
    source = data / "recordings.sqlite3"
    if not source.is_file() or source.is_symlink():
        raise ValueError("Existing local database required")
    backups.mkdir(parents=True, exist_ok=True, mode=0o700)
    estimate = (
        source.stat().st_size
        + sum((config / name).stat().st_size for name in CONFIG_FILES)
        + 1024**2
    )
    storage.check_free(backups, estimate)
    folder = backups / datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    folder.mkdir(mode=0o700)
    destination = folder / "recordings.sqlite3"
    with (
        sqlite3.connect(f"file:{source}?mode=ro", uri=True) as old,
        sqlite3.connect(destination) as new,
    ):
        old.backup(new, pages=256)
    destination.chmod(0o600)
    check_database(destination)
    for name in CONFIG_FILES:
        value = read_private_json(config / name)
        write_private_json(folder / name, value)
    names = ("recordings.sqlite3", *CONFIG_FILES)
    write_private_json(
        folder / "manifest.json",
        {
            "format": 1,
            "created": datetime.now(UTC).isoformat(),
            "revision": os.environ.get("V360_REVISION", "working-tree"),
            "sha256": {name: digest(folder / name) for name in names},
            "recording_bytes_included": False,
        },
    )
    verify(folder)
    return folder


def restore(folder, data, config, backups):
    folder, data, config = folder.resolve(), data.resolve(), config.resolve()
    verify(folder)
    # The caller must have stopped the service and mounted config writable for this explicit operation.
    with InstanceLock(data):
        current = data / "recordings.sqlite3"
        if not current.is_file():
            raise ValueError("Restore targets an existing stopped deployment")
        with sqlite3.connect(f"file:{folder / 'recordings.sqlite3'}?mode=ro", uri=True) as db:
            for (relative,) in db.execute(
                "SELECT path FROM recordings WHERE state='verified' AND path IS NOT NULL"
            ):
                path = (data / relative).resolve()
                if not path.is_relative_to(data) or not path.is_file():
                    raise ValueError(
                        "A recording referenced by the backup is missing; restore refused"
                    )
        safety = backup(data, config, backups)
        # Test all config destinations before replacing any file.
        for name in CONFIG_FILES:
            target = config / (name + ".restore-check")
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
            target.unlink()
        for sidecar in ("-wal", "-shm", "-journal"):
            if (data / ("recordings.sqlite3" + sidecar)).exists():
                raise ValueError(
                    "Database journal files remain; inspect the stopped deployment first"
                )
        temporary = data / "recordings.restore"
        with temporary.open("xb") as target, (folder / "recordings.sqlite3").open("rb") as source:
            shutil.copyfileobj(source, target)
        temporary.chmod(0o600)
        # A restored archive stays paused until an operator has reviewed it.
        with sqlite3.connect(temporary) as db:
            # Restoring metadata must not resurrect a previously revoked browser login.
            if db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='browser_sessions'"
            ).fetchone():
                db.execute("DELETE FROM browser_sessions")
            row = db.execute("SELECT body FROM backup_state WHERE id='policy'").fetchone()
            if row:
                policy = json.loads(row[0])
                policy["enabled"] = False
                db.execute(
                    "UPDATE backup_state SET body=? WHERE id='policy'", (json.dumps(policy),)
                )
        for name in CONFIG_FILES:
            target = config / (name + ".restore")
            write_private_json(target, read_private_json(folder / name))
        temporary.replace(current)
        for name in CONFIG_FILES:
            (config / (name + ".restore")).replace(config / name)
    return {
        "restored": True,
        "automatic_backup": "paused",
        "safety_backup": safety.name,
        "media_files": "preserved; files added since snapshot may need index reconciliation",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("backup", "restore"):
        sub = commands.add_parser(name)
        sub.add_argument("--data", type=Path, required=True)
        sub.add_argument("--config", type=Path, required=True)
        sub.add_argument("--backups", type=Path, required=True)
        if name == "restore":
            sub.add_argument("--snapshot", type=Path, required=True)
    sub = commands.add_parser("verify")
    sub.add_argument("snapshot", type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if args.command == "backup":
            folder = backup(args.data, args.config, args.backups)
            result = {"backup": folder.name, **verify(folder)}
        elif args.command == "restore":
            result = restore(args.snapshot, args.data, args.config, args.backups)
        else:
            result = verify(args.snapshot)
        print(json.dumps(result))
    except (CameraError, OSError, ValueError, sqlite3.Error):
        print(
            json.dumps(
                {
                    "error": "operation_refused",
                    "message": "Inspect paths, permissions, service ownership and backup integrity.",
                }
            )
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
