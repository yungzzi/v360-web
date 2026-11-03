from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from v360_web import publication
from v360_web.api import create_app
from v360_web.protocol import RecordingEvent

TOKEN = "test-batch-token-1234567890123456789012345678"


def item(store, start, duration=0):
    key = store.discover("test", 0, [RecordingEvent(datetime.fromisoformat(start), 0, 0, 0, duration)])[0]
    return store.get(key)


@pytest.mark.parametrize("start,duration,batch,release", [
    ("2026-11-03T12:00:00", 0, "12:00", "13:00"),
    ("2026-11-03T12:29:50", 20, "12:30", "13:30"),
    ("2026-11-03T23:59:59", 0, "23:30", "00:30"),
])
def test_half_hour_windows_follow_end_and_cross_midnight(tmp_path, start, duration, batch, release):
    store = publication.Publication(tmp_path)
    a, _, c = publication.window(item(store, start, duration))
    assert a.strftime("%H:%M") == batch and c.strftime("%H:%M") == release
    if batch == "23:30":
        assert c.day == 4


def test_five_attempts_persist_device_failures_refund_manual_does_not_reset(tmp_path, monkeypatch):
    monkeypatch.setattr(publication, "now_local", lambda: datetime(2026, 11, 3, 12, 40))
    store = publication.Publication(tmp_path);store.enable(True)
    row = item(store, "2026-11-03T12:10:00")
    assert store.begin(row["id"])
    store.device_unavailable(row["id"])
    assert store.info(row)["attempts"] == 0
    for _ in range(5):
        assert store.begin(row["id"])
    store = publication.Publication(tmp_path)
    assert not store.begin(row["id"]) and not store.allowed(row)
    assert store.begin(row["id"], manual=True)
    assert store.info(row)["attempts"] == 5 and store.info(row)["manual_attempts"] == 1


def test_at_release_window_closes_but_manual_retry_remains_possible(tmp_path, monkeypatch):
    monkeypatch.setattr(publication, "now_local", lambda: datetime(2026, 11, 3, 13))
    store = publication.Publication(tmp_path);store.enable(True)
    row = item(store, "2026-11-03T12:10:00")
    assert store.published(row) and not store.allowed(row)
    assert not store.begin(row["id"])
    assert store.info(row)["stopped_reason"] == "window_closed"
    assert store.begin(row["id"], manual=True)


def test_all_recording_media_routes_reject_unpublished_batches(tmp_path, monkeypatch):
    monkeypatch.setattr(publication, "now_local", lambda: datetime(2026, 11, 3, 12, 40))
    store = publication.Publication(tmp_path);store.enable(True)
    row = item(store, "2026-11-03T12:10:00")
    with TestClient(create_app(tmp_path, TOKEN)) as client:
        for suffix in ("file", "preview", "window?start=0"):
            response = client.get('/api/recordings/'+row['id']+'/'+suffix, headers={"Authorization": "Bearer "+TOKEN})
            assert response.status_code == 409
        assert client.get('/api/publication?day=2026-11-03').status_code == 401
        report = client.get('/api/publication?day=2026-11-03', headers={"Authorization": "Bearer "+TOKEN}).json()
        assert report['batches'][0]['release_at'] == '2026-11-03T13:00:00'


def test_fifth_successful_transfer_can_still_prepare_before_release(tmp_path, monkeypatch):
    monkeypatch.setattr(publication, "now_local", lambda: datetime(2026, 11, 3, 12, 40))
    store = publication.Publication(tmp_path);store.enable(True)
    row = item(store, "2026-11-03T12:10:00")
    for _ in range(5):
        assert store.begin(row["id"])
    store.update(row["id"], "downloaded_unverified")
    assert store.allowed(store.get(row["id"]))
    assert not store.begin(row["id"])
