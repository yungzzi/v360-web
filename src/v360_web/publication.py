"""Half-hour publication windows and durable bounded automatic attempts."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .store import RecordingStore

ZONE = ZoneInfo("Asia/Hong_Kong")


def now_local():
    return datetime.now(ZONE).replace(tzinfo=None)


def window(item):
    end = datetime.fromisoformat(item["start_local"]) + timedelta(seconds=max(0, min(item["duration_raw"], 3600)))
    start = end.replace(minute=end.minute // 30 * 30, second=0, microsecond=0)
    return start, start + timedelta(minutes=30), start + timedelta(hours=1)


def final_check_window(now=None):
    now = now or now_local()
    if now.minute % 30 < 25:
        return None
    release = now.replace(minute=now.minute // 30 * 30, second=0, microsecond=0) + timedelta(minutes=30)
    return release - timedelta(hours=1), release - timedelta(minutes=30), release


class Publication(RecordingStore):
    def __init__(self, root):
        super().__init__(root)
        with self.connection() as db:
            db.execute("CREATE TABLE IF NOT EXISTS publication_settings (id INTEGER PRIMARY KEY, enabled INTEGER)")
            db.execute("CREATE TABLE IF NOT EXISTS publication_attempts (id TEXT PRIMARY KEY, attempts INTEGER DEFAULT 0, manual_attempts INTEGER DEFAULT 0, reason TEXT)")

    def enabled(self):
        with self.connection() as db:
            row = db.execute("SELECT enabled FROM publication_settings WHERE id=1").fetchone()
        return bool(row and row[0])

    def enable(self, value):
        with self.connection() as db:
            db.execute("INSERT OR REPLACE INTO publication_settings VALUES (1,?)", (bool(value),))

    def info(self, item, now=None):
        start, end, release = window(item)
        with self.connection() as db:
            row = db.execute("SELECT * FROM publication_attempts WHERE id=?", (item["id"],)).fetchone()
        attempts = row["attempts"] if row else 0
        expired = (now or now_local()) >= release
        return {"batch_start": start.isoformat(), "batch_end": end.isoformat(), "release_at": release.isoformat(),
                "released": expired, "attempts": attempts, "manual_attempts": row["manual_attempts"] if row else 0,
                "stopped_reason": (row["reason"] if row else None) or ("attempt_limit" if attempts >= 5 and item["state"] not in ("verified", "downloaded_unverified") else "window_closed" if expired else None)}

    def allowed(self, item):
        return bool(item) and (not self.enabled() or self.info(item)["stopped_reason"] is None)

    def published(self, item):
        return not self.enabled() or self.info(item)["released"]

    def begin(self, key, manual=False):
        with self.connection() as db:
            db.execute("INSERT OR IGNORE INTO publication_attempts(id) VALUES (?)", (key,))
            if manual:
                db.execute("UPDATE publication_attempts SET manual_attempts=manual_attempts+1 WHERE id=?", (key,))
                return True
            item = db.execute("SELECT * FROM recordings WHERE id=?", (key,)).fetchone()
            if item is None or now_local() >= window(item)[2]:
                return False
            result = db.execute("UPDATE publication_attempts SET attempts=attempts+1 WHERE id=? AND attempts<5 AND reason IS NULL", (key,))
            return result.rowcount == 1

    def device_unavailable(self, key):
        # Device-wide connection failures do not consume each file's attempt budget.
        with self.connection() as db:
            db.execute("UPDATE publication_attempts SET attempts=max(0,attempts-1) WHERE id=?", (key,))


def retry_once(config, key):
    from . import playback, service
    from .config import Settings
    from .errors import CameraError
    settings = Settings.load(config)
    store = Publication(settings.data_root)
    item = store.get(key)
    if not item or item["device_id"] != settings.did:
        raise CameraError("recording_missing", "Recording is not indexed for this device.")
    store.begin(key, manual=True)
    service.download(config, key)
    return playback.prepare(config, key)
