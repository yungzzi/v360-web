from contextlib import contextmanager
from datetime import datetime

from fastapi.testclient import TestClient

from v360_web.api import create_app
from v360_web.protocol import RecordingEvent
from v360_web.publication import Publication
from v360_web.store import RecordingStore


def test_metadata_requests_use_bounded_database_connections(tmp_path, monkeypatch):
    store = Publication(tmp_path)
    store.enable(True)
    store.discover("test", 0, [RecordingEvent(datetime(2026, 1, 1, 12, n // 60, n % 60), 0, 0, 0, 3) for n in range(200)])
    app = create_app(tmp_path, "test-performance-token-123456789012345678901")
    original = RecordingStore.connection
    calls = []

    @contextmanager
    def counted(self):
        calls.append(1)
        with original(self) as db:
            yield db

    with TestClient(app) as client:
        monkeypatch.setattr(RecordingStore, "connection", counted)
        for endpoint in ("/api/library/timeline?day=2026-01-01", "/api/publication?day=2026-01-01"):
            calls.clear()
            response = client.get(endpoint, headers={"Authorization": "Bearer test-performance-token-123456789012345678901"})
            assert response.status_code == 200
            assert len(calls) < 20, (endpoint, len(calls))
            assert "device_id" not in response.text and '"path"' not in response.text
