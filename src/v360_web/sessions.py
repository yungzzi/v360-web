"""Revocable browser sessions; store only digests, bound to the current web token."""

import hashlib
import secrets
import time

from .store import RecordingStore

COOKIE = "v360_session"
REMEMBER_SECONDS = 7 * 86400
SESSION_SECONDS = 12 * 3600


class SessionStore(RecordingStore):
    def __init__(self, root, access_token):
        super().__init__(root)
        self.fingerprint = self.digest(access_token)
        with self.connection() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS browser_sessions
                (digest TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, expires REAL NOT NULL)""")

    @staticmethod
    def digest(value):
        return hashlib.sha256(value.encode()).hexdigest()

    def issue(self, remember, previous=""):
        now = time.time()
        duration = REMEMBER_SECONDS if remember else SESSION_SECONDS
        key = secrets.token_urlsafe(32)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "DELETE FROM browser_sessions WHERE expires<=? OR fingerprint<>?",
                (now, self.fingerprint),
            )
            if previous:
                db.execute("DELETE FROM browser_sessions WHERE digest=?", (self.digest(previous),))
            if db.execute("SELECT COUNT(*) FROM browser_sessions").fetchone()[0] >= 32:
                return None
            db.execute(
                "INSERT INTO browser_sessions VALUES (?,?,?)",
                (self.digest(key), self.fingerprint, now + duration),
            )
        return key

    def valid(self, key):
        if not isinstance(key, str) or not 32 <= len(key) <= 128:
            return False
        with self.connection() as db:
            return (
                db.execute(
                    "SELECT 1 FROM browser_sessions WHERE digest=? AND fingerprint=? AND expires>?",
                    (self.digest(key), self.fingerprint, time.time()),
                ).fetchone()
                is not None
            )

    def revoke(self, key):
        with self.connection() as db:
            db.execute("DELETE FROM browser_sessions WHERE digest=?", (self.digest(key),))
