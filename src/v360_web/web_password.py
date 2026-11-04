"""Separate, Unicode browser password. API token remains unchanged."""
import hashlib
import hmac
import secrets

from .store import RecordingStore


class WebPassword(RecordingStore):
    def __init__(self, root):
        super().__init__(root)
        with self.connection() as db:
            db.execute("CREATE TABLE IF NOT EXISTS web_password (id INTEGER PRIMARY KEY, salt TEXT, digest TEXT)")

    def value(self):
        with self.connection() as db:
            row = db.execute("SELECT salt,digest FROM web_password WHERE id=1").fetchone()
        return tuple(row) if row else None

    def binding(self):
        value = self.value()
        return ':'.join(value) if value else ''

    def verify(self, candidate, token):
        if not isinstance(candidate, str) or len(candidate) > 256:
            return False
        value = self.value()
        if value is None:
            return hmac.compare_digest(candidate.encode('utf-8'), token.encode('utf-8'))
        salt, expected = value
        actual = hashlib.pbkdf2_hmac('sha256', candidate.encode('utf-8'), bytes.fromhex(salt), 210000).hex()
        return hmac.compare_digest(actual, expected)

    def change(self, candidate):
        if not 8 <= len(candidate) <= 128:
            raise ValueError('Use 8 to 128 characters')
        salt = secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac('sha256', candidate.encode('utf-8'), salt, 210000).hex()
        with self.connection() as db:
            db.execute("INSERT OR REPLACE INTO web_password VALUES (1,?,?)", (salt.hex(),digest))
