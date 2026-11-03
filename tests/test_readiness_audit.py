import hashlib
import json
import runpy
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from v360_web.backup_policy import BackupPolicy
from v360_web.protocol import RecordingEvent
from v360_web.store import RecordingStore


def test_readiness_audit_checks_source_and_http_not_just_saved_state(tmp_path, monkeypatch):
    audit = runpy.run_path(str(Path(__file__).resolve().parents[1] / "tools/audit_readiness.py"))[
        "snapshot"
    ]
    root = tmp_path / "data"
    store = RecordingStore(root)
    BackupPolicy(root)
    config = tmp_path / "config"
    config.mkdir()
    (config / "web.json").write_text(json.dumps({"access_token": "synthetic-test-only-token"}))
    now = datetime.now(ZoneInfo("Asia/Hong_Kong")).replace(tzinfo=None)
    keys = store.discover(
        "test", 0, [RecordingEvent(now - timedelta(hours=n), 0, 0, 0, 15) for n in (2, 3)]
    )
    media = root / "media"
    media.mkdir()
    previews = root / "previews"
    previews.mkdir()
    for i, key in enumerate(keys):
        source = media / (key + ".media")
        source.write_bytes(b"original")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        store.update(key, "verified", path=str(source.relative_to(root)), bytes=8, sha256=digest)
        target = previews / (key + ".mp4")
        target.write_bytes(b"test-mp4")
        (previews / (key + ".json")).write_text(
            json.dumps(
                {
                    "verified": True,
                    "source_sha256": digest if i == 0 else "wrong",
                    "bytes": target.stat().st_size,
                }
            )
        )

    class Response:
        status = 206

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, size):
            return b"test-mp4"

    requests = []

    def get(request, timeout):
        requests.append(request.full_url)
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", get)
    before = (root / "recordings.sqlite3").stat().st_mtime_ns
    result = audit(tmp_path)
    assert result["known"] == 2 and result["full_ready"] == 1
    assert result["overdue_missing"] == 1 and result["http_verified"] == 1
    assert len(requests) == 1 and keys[0] in requests[0]
    assert (root / "recordings.sqlite3").stat().st_mtime_ns == before


def test_observer_records_database_interruption_and_can_resume(tmp_path, monkeypatch):
    import sqlite3

    observe = runpy.run_path(str(Path(__file__).resolve().parents[1] / "tools/audit_readiness.py"))["observe"]
    def busy(*args):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setitem(observe.__globals__, "snapshot", busy)
    result = observe(tmp_path, set())
    assert result["acceptance"] == "observation_failed"
    assert result["observation_error"] == "database_unavailable"
    assert "full_ready" not in result and "overdue_missing" not in result
    recovered = {"acceptance": "pending_evidence_review", "overdue_missing": 2}
    monkeypatch.setitem(observe.__globals__, "snapshot", lambda *args: recovered)
    assert observe(tmp_path, set()) == recovered
