import json

from .store import RecordingStore


class JobStore(RecordingStore):
    def __init__(self, root):
        super().__init__(root)
        with self.connection() as db:
            db.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, body TEXT NOT NULL)")

    def get_job(self, key):
        with self.connection() as db:
            row = db.execute("SELECT body FROM jobs WHERE id=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def update_job(self, key, **values):
        with self.connection() as db:
            row = db.execute("SELECT body FROM jobs WHERE id=?", (key,)).fetchone()
            value = json.loads(row[0]) if row else {"id": key}
            value.update(values, updated_at=self.timestamp())
            db.execute(
                "INSERT INTO jobs VALUES (?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body",
                (key, json.dumps(value)),
            )

    def recover_interrupted(self):
        with self.connection() as db:
            db.execute(
                "UPDATE recordings SET state='failed',error='interrupted' WHERE state='downloading'"
            )
            for row in db.execute("SELECT id,body FROM jobs").fetchall():
                value = json.loads(row[1])
                if value["state"] in ("running", "queued"):
                    value.update(
                        state="failed",
                        error="interrupted",
                        message="Service restarted; retry the operation.",
                    )
                    db.execute("UPDATE jobs SET body=? WHERE id=?", (json.dumps(value), row[0]))

    def recent(self):
        with self.connection() as db:
            return [
                json.loads(row[0])
                for row in db.execute("SELECT body FROM jobs ORDER BY rowid DESC LIMIT 20")
            ]
