from __future__ import annotations

import hashlib
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path


class RecordingStore:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.database = self.root / "recordings.sqlite3"
        with self.connection() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS recordings (
                id TEXT PRIMARY KEY, device_id TEXT NOT NULL, channel INTEGER NOT NULL,
                start_local TEXT NOT NULL, duration_raw INTEGER NOT NULL,
                event_type INTEGER NOT NULL, remote_status INTEGER NOT NULL, weekday INTEGER NOT NULL,
                state TEXT NOT NULL DEFAULT 'discovered', path TEXT,
                bytes INTEGER NOT NULL DEFAULT 0, sha256 TEXT, error TEXT,
                updated_at TEXT NOT NULL)""")

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.database, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        finally:
            db.close()

    @staticmethod
    def timestamp():
        return datetime.now(UTC).isoformat()

    def discover(self, did: str, channel: int, events: list):
        ids = []
        with self.connection() as db:
            for event in events:
                identity = f"{did}|{channel}|{event.start.isoformat()}|{event.event_type}"
                key = hashlib.sha256(identity.encode()).hexdigest()[:32]
                db.execute(
                    """INSERT INTO recordings
                    (id,device_id,channel,start_local,duration_raw,event_type,remote_status,weekday,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                    duration_raw=excluded.duration_raw,remote_status=excluded.remote_status,
                    updated_at=excluded.updated_at""",
                    (
                        key,
                        did,
                        channel,
                        event.start.isoformat(),
                        event.duration_raw,
                        event.event_type,
                        event.status,
                        event.weekday,
                        self.timestamp(),
                    ),
                )
                ids.append(key)
        return ids

    def list(self):
        with self.connection() as db:
            return [
                dict(row)
                for row in db.execute("SELECT * FROM recordings ORDER BY start_local DESC")
            ]

    def get(self, key: str):
        with self.connection() as db:
            row = db.execute("SELECT * FROM recordings WHERE id=?", (key,)).fetchone()
            return dict(row) if row else None

    def update(self, key: str, state: str, **values):
        allowed = {"path", "bytes", "sha256", "error"}
        if not set(values) <= allowed:
            raise ValueError("Unsupported recording update")
        updates = {"state": state, "updated_at": self.timestamp(), **values}
        with self.connection() as db:
            assignments = ",".join(f"{name}=?" for name in updates)
            db.execute(f"UPDATE recordings SET {assignments} WHERE id=?", [*updates.values(), key])

    def media_path(self, key: str):
        item = self.get(key)
        if not item or item["state"] != "verified" or not item["path"]:
            return None
        path = (self.root / item["path"]).resolve()
        if not path.is_relative_to(self.root) or not path.is_file():
            return None
        return path
