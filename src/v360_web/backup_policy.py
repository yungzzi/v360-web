import json

from .store import RecordingStore

PROFILES = {
    "local": {
        "archive_limit": 128 * 1024**2,
        "batch_files": 2,
        "batch_bytes": 8 * 1024**2,
        "daily_bytes": 16 * 1024**2,
    },
    "server": {
        "archive_limit": 10 * 1024**3,
        "batch_files": 10,
        "batch_bytes": 64 * 1024**2,
        "daily_bytes": 512 * 1024**2,
    },
}
DEFAULT = {"enabled": True, "mode": "local", "interval_minutes": 60, "retention_days": 30}


class BackupPolicy(RecordingStore):
    def __init__(self, root):
        super().__init__(root)
        with self.connection() as db:
            db.execute("CREATE TABLE IF NOT EXISTS backup_state (id TEXT PRIMARY KEY, body TEXT)")
            db.execute("""CREATE TABLE IF NOT EXISTS backup_retries
                (id TEXT PRIMARY KEY, attempts INTEGER, next_try REAL)""")

    def read(self, key, default=None):
        with self.connection() as db:
            row = db.execute("SELECT body FROM backup_state WHERE id=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def write(self, key, value):
        with self.connection() as db:
            db.execute("INSERT OR REPLACE INTO backup_state VALUES (?,?)", (key, json.dumps(value)))

    def policy(self):
        value = {**DEFAULT, **self.read("policy", {})}
        return {**value, **PROFILES[value["mode"]]}
