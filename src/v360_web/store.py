from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

MIGRATION_LOCK = threading.RLock()


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
            db.execute("""CREATE TABLE IF NOT EXISTS catalog_checks (
                device_id TEXT, start_local TEXT, end_local TEXT, checked_at TEXT,
                diagnostics TEXT, PRIMARY KEY(device_id,start_local,end_local))""")
        with MIGRATION_LOCK, self.connection() as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(recordings)")}
            for name, definition in [
                ("pinned", "INTEGER NOT NULL DEFAULT 0"),
                ("saved_at", "TEXT"),
            ]:
                if name not in columns:
                    db.execute(f"ALTER TABLE recordings ADD COLUMN {name} {definition}")
            if "saved_at" not in columns:
                db.execute(
                    "UPDATE recordings SET saved_at=updated_at WHERE state='verified' AND saved_at IS NULL"
                )
            db.execute("CREATE TABLE IF NOT EXISTS media_leases (id TEXT PRIMARY KEY, until REAL)")

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

    def library(self, day=None, offset=0, limit=30, did=None):
        clauses, parameters = [], []
        if day is not None:
            clauses.append("substr(start_local,1,10)=?")
            parameters.append(day.isoformat())
        if did is not None:
            clauses.append("device_id=?")
            parameters.append(did)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.connection() as db:
            total = db.execute("SELECT count(*) FROM recordings" + where, parameters).fetchone()[0]
            rows = db.execute(
                "SELECT * FROM recordings"
                + where
                + " ORDER BY start_local DESC,id LIMIT ? OFFSET ?",
                [*parameters, limit, offset],
            ).fetchall()
        return total, [dict(row) for row in rows]

    def save_catalog(self, did, start, end, diagnostics):
        with self.connection() as db:
            db.execute(
                """INSERT OR REPLACE INTO catalog_checks VALUES (?,?,?,?,?)""",
                (
                    did,
                    start.isoformat(),
                    end.isoformat(),
                    self.timestamp(),
                    json.dumps(diagnostics),
                ),
            )

    def catalog(self, did, day):
        if not did or not day:
            return None
        with self.connection() as db:
            row = db.execute(
                """SELECT checked_at,diagnostics FROM catalog_checks
                WHERE device_id=? AND start_local<=? AND end_local>=?
                ORDER BY checked_at DESC LIMIT 1""",
                (did, day.isoformat() + "T00:00:00", day.isoformat() + "T23:59:59"),
            ).fetchone()
        return {"checked_at": row[0], **json.loads(row[1])} if row else None

    def get(self, key: str):
        with self.connection() as db:
            row = db.execute("SELECT * FROM recordings WHERE id=?", (key,)).fetchone()
            return dict(row) if row else None

    def update(self, key: str, state: str, **values):
        allowed = {"path", "bytes", "sha256", "error"}
        if not set(values) <= allowed:
            raise ValueError("Unsupported recording update")
        updates = {"state": state, "updated_at": self.timestamp(), **values}
        if state == "verified":
            updates["saved_at"] = self.timestamp()
        with self.connection() as db:
            assignments = ",".join(f"{name}=?" for name in updates)
            db.execute(f"UPDATE recordings SET {assignments} WHERE id=?", [*updates.values(), key])

    def pin(self, key, value):
        with self.connection() as db:
            return db.execute(
                "UPDATE recordings SET pinned=? WHERE id=?", (int(value), key)
            ).rowcount

    def lease(self, key, seconds=300):
        with self.connection() as db:
            db.execute(
                "INSERT OR REPLACE INTO media_leases VALUES (?,?)", (key, time.time() + seconds)
            )

    def leased(self, key):
        with self.connection() as db:
            row = db.execute("SELECT until FROM media_leases WHERE id=?", (key,)).fetchone()
        return bool(row and row[0] > time.time())

    def media_path(self, key: str):
        item = self.get(key)
        if not item or item["state"] != "verified" or not item["path"]:
            return None
        path = (self.root / item["path"]).resolve()
        if not path.is_relative_to(self.root) or not path.is_file():
            return None
        return path
