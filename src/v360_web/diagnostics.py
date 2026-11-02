"""Bounded connection/stage evidence. Never accepts credentials or SDK log text."""

import json
import sqlite3
import time


def record(root, stage, **values):
    if root is None:
        return
    allowed = {"attempt", "elapsed", "sdk_code", "error", "ok", "key", "forced", "download_header"}
    safe = {k: v for k, v in values.items() if k in allowed}
    try:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with sqlite3.connect(root / "recordings.sqlite3", timeout=10) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS camera_events (id INTEGER PRIMARY KEY, at REAL, stage TEXT, details TEXT)"
            )
            db.execute(
                "INSERT INTO camera_events(at,stage,details) VALUES (?,?,?)",
                (time.time(), stage, json.dumps(safe)),
            )
            db.execute(
                "DELETE FROM camera_events WHERE id <= (SELECT coalesce(max(id),0)-10000 FROM camera_events)"
            )
    except (OSError, sqlite3.Error):
        # Diagnostics must never strand a camera session or hide its original error.
        return
