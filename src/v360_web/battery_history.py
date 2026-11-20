"""Battery readings kept for overnight-drain review.

Rows are written from the reading a watching or scheduled session already takes
on its existing camera connection: no extra handshake, no extra query. Every row
is de-duplicated against the previous one, capped per device, and pruned by age.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ZONE = ZoneInfo("Asia/Hong_Kong")
DEDUPE_SECONDS = 240
RETENTION_DAYS = 30
MAX_ROWS_PER_DEVICE = 12000


def now_local():
    return datetime.now(ZONE).replace(tzinfo=None)


class BatteryHistory:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.database = self.root / "recordings.sqlite3"
        with self.connection() as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS battery_history (
                device_id TEXT NOT NULL, captured_at TEXT NOT NULL,
                percent INTEGER, charging INTEGER,
                PRIMARY KEY (device_id, captured_at))"""
            )

    @contextmanager
    def connection(self):
        # Short busy timeout: a reading must never stall the control loop that
        # shares this connection with live viewing.
        db = sqlite3.connect(self.database, timeout=2)
        try:
            yield db
            db.commit()
        finally:
            db.close()

    def record(self, did, percent, charging, at=None):
        """Store one reading; return False when it is a duplicate or unusable."""
        if not did or percent is None:
            return False
        moment = (at or now_local()).replace(microsecond=0)
        flag = None if charging is None else int(bool(charging))
        with self.connection() as db:
            last = db.execute(
                "SELECT captured_at,percent,charging FROM battery_history "
                "WHERE device_id=? ORDER BY captured_at DESC LIMIT 1",
                (did,),
            ).fetchone()
            if last is not None and (last[1], last[2]) == (int(percent), flag):
                gap = (moment - datetime.fromisoformat(last[0])).total_seconds()
                if 0 <= gap < DEDUPE_SECONDS:
                    return False
            cursor = db.execute(
                "INSERT OR IGNORE INTO battery_history VALUES (?,?,?,?)",
                (did, moment.isoformat(), int(percent), flag),
            )
            if not cursor.rowcount:
                return False
            self._prune(db, did, moment)
            return True

    def _prune(self, db, did, moment):
        cutoff = (moment - timedelta(days=RETENTION_DAYS)).isoformat()
        db.execute(
            "DELETE FROM battery_history WHERE device_id=? AND captured_at<?", (did, cutoff)
        )
        count = db.execute(
            "SELECT count(*) FROM battery_history WHERE device_id=?", (did,)
        ).fetchone()[0]
        if count > MAX_ROWS_PER_DEVICE:
            db.execute(
                "DELETE FROM battery_history WHERE rowid IN (SELECT rowid FROM battery_history "
                "WHERE device_id=? ORDER BY captured_at DESC LIMIT -1 OFFSET ?)",
                (did, MAX_ROWS_PER_DEVICE),
            )

    def day(self, day, did=None):
        """Readings for one local calendar day, oldest first."""
        clauses = ["captured_at>=?", "captured_at<?"]
        parameters = [day.isoformat(), (day + timedelta(days=1)).isoformat()]
        if did:
            clauses.append("device_id=?")
            parameters.append(did)
        with self.connection() as db:
            rows = db.execute(
                "SELECT device_id,captured_at,percent,charging FROM battery_history WHERE "
                + " AND ".join(clauses)
                + " ORDER BY captured_at",
                parameters,
            ).fetchall()
        return [
            {
                "device_id": row[0],
                "at": row[1],
                "percent": row[2],
                "charging": None if row[3] is None else bool(row[3]),
            }
            for row in rows
        ]

    def latest(self, did):
        with self.connection() as db:
            row = db.execute(
                "SELECT captured_at,percent,charging FROM battery_history "
                "WHERE device_id=? ORDER BY captured_at DESC LIMIT 1",
                (did,),
            ).fetchone()
        if row is None:
            return None
        return {
            "at": row[0],
            "percent": row[1],
            "charging": None if row[2] is None else bool(row[2]),
        }


_HISTORY = {}
_HISTORY_GUARD = threading.Lock()


def history_for(root):
    """One writer per data directory; each call still opens its own connection."""
    key = str(Path(root).resolve())
    with _HISTORY_GUARD:
        history = _HISTORY.get(key)
        if history is None:
            history = _HISTORY[key] = BatteryHistory(key)
        return history
