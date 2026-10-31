import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from v360_web import production, progress, service, storage
from v360_web.api import create_app
from v360_web.backup_policy import BackupPolicy
from v360_web.config import write_private_json
from v360_web.errors import CameraError
from v360_web.instance import InstanceLock
from v360_web.media import run_media
from v360_web.operations import backup, restore, verify
from v360_web.protocol import RecordingEvent
from v360_web.store import RecordingStore

TOKEN = "deployment-test-token-12345678901234567890"


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(storage.shutil, "disk_usage", lambda _: SimpleNamespace(free=100 * 1024**3))
    data, config, snapshots = [tmp_path / p for p in ("data", "config", "backups")]
    policy = BackupPolicy(data)
    policy.write("policy", {"enabled": False, "mode": "local"})
    key = policy.discover("MATE-test", 0, [RecordingEvent(datetime(2026, 10, 29, 12), 0, 2, 1, 1)])[
        0
    ]
    (data / "media").mkdir()
    (data / "media/example.media").write_bytes(b"original-recording")
    policy.update(key, "verified", path="media/example.media", bytes=18)
    write_private_json(
        config / "device.json",
        {
            "device": {"did": "MATE-test", "password": "example"},
            "bridge": {"adapter": "native-arm64"},
            "data_root": "../data",
        },
    )
    write_private_json(config / "sdk-profiles.json", {"initialize": "example-profile"})
    write_private_json(config / "web.json", {"access_token": TOKEN})
    return data, config, snapshots, key


def test_instance_lock_cross_process_and_releases(tmp_path):
    with InstanceLock(tmp_path):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                'from pathlib import Path; from v360_web.instance import InstanceLock; InstanceLock(Path(__import__("sys").argv[1])).__enter__()',
                str(tmp_path),
            ],
            capture_output=True,
            check=False,
            text=True,
        )
        assert result.returncode != 0 and "Another service owns" in result.stderr
    with InstanceLock(tmp_path):
        pass


def test_instance_lock_rejects_symlink(tmp_path):
    target = tmp_path / "preserve"
    target.write_text("keep")
    (tmp_path / ".service.lock").symlink_to(target)
    with pytest.raises(CameraError), InstanceLock(tmp_path):
        pass
    assert target.read_text() == "keep"


def test_snapshot_restore_preserves_media_and_pauses(deployment):
    data, config, snapshots, key = deployment
    folder = backup(data, config, snapshots)
    assert verify(folder)["records"] == 1
    assert not (folder / "media").exists()
    store = RecordingStore(data)
    store.pin(key, True)
    result = restore(folder, data, config, snapshots)
    assert result["restored"] and not store.get(key)["pinned"]
    assert not BackupPolicy(data).policy()["enabled"]
    assert (data / "media/example.media").read_bytes() == b"original-recording"
    assert len(list(snapshots.iterdir())) == 2


def test_restore_refuses_running_service_missing_media_or_bad_hash(deployment):
    data, config, snapshots, key = deployment
    folder = backup(data, config, snapshots)
    with InstanceLock(data), pytest.raises(CameraError):
        restore(folder, data, config, snapshots)
    (data / "media/example.media").unlink()
    with pytest.raises(ValueError, match="recording"):
        restore(folder, data, config, snapshots)
    (folder / "web.json").write_text("{}")
    with pytest.raises(ValueError, match="hash"):
        verify(folder)
    assert RecordingStore(data).get(key)["state"] == "verified"


def test_backup_while_writing_is_consistent(deployment):
    data, config, snapshots, key = deployment
    store = RecordingStore(data)
    done = threading.Event()

    def mutate():
        while not done.is_set():
            store.pin(key, True)
            store.pin(key, False)
            time.sleep(0.005)

    thread = threading.Thread(target=mutate)
    thread.start()
    try:
        folder = backup(data, config, snapshots)
        assert verify(folder)["verified"]
    finally:
        done.set()
        thread.join(timeout=3)


def test_readiness_and_production_disables_local_shutdown(deployment, monkeypatch):
    data, config, _snapshots, _key = deployment
    monkeypatch.setenv("V360_REVISION", "tested-revision")
    app = create_app(data, TOKEN, config / "device.json", start_backup=True)
    with TestClient(app) as client:
        value = client.get("/ready").json()
        assert value["ready"] and value["scheduler_started"] and not value["camera_checked"]
        assert value["revision"] == "tested-revision"
        headers = {"Authorization": "Bearer " + TOKEN}
        assert not client.get("/api/status", headers=headers).json()["local_shutdown_available"]
        assert client.post("/api/local/shutdown", headers=headers).status_code == 403


def test_shutdown_cancels_active_manual_job(tmp_path, monkeypatch):
    active = threading.Event()
    stopped = threading.Event()

    def download(*args):
        active.set()
        try:
            while True:
                progress.check_cancelled()
                time.sleep(0.01)
        finally:
            stopped.set()

    monkeypatch.setattr(service, "download", download)
    with TestClient(create_app(tmp_path, TOKEN, tmp_path / "unused.json")) as client:
        assert (
            client.post(
                "/api/jobs/download/example", headers={"Authorization": "Bearer " + TOKEN}
            ).status_code
            == 202
        )
        assert active.wait(timeout=2)
    assert stopped.is_set()


def test_shutdown_terminates_owned_media_process():
    event = threading.Event()
    timer = threading.Timer(0.15, event.set)
    context = progress.shutdown_event.set(event)
    timer.start()
    before = time.monotonic()
    try:
        with pytest.raises(CameraError) as error:
            run_media(
                [sys.executable, "-c", "import time;time.sleep(120)"],
                capture_output=True,
                check=False,
                timeout=120,
            )
        assert error.value.code == "interrupted"
        assert time.monotonic() - before < 3
    finally:
        progress.shutdown_event.reset(context)
        timer.join()


def test_secure_media_cookie_configuration(tmp_path, monkeypatch):
    monkeypatch.setenv("V360_SECURE_COOKIES", "1")
    with TestClient(create_app(tmp_path, TOKEN)) as client:
        response = client.post(
            "/api/playback/session", headers={"Authorization": "Bearer " + TOKEN}
        )
        assert "Secure" in response.headers["set-cookie"].split("; ")


def test_release_rejects_unversioned_image_before_docker(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "release_tool", Path(__file__).resolve().parents[1] / "tools/release.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for image in ("v360-web", "user/v360-web:latest", "bad name:v2"):
        with pytest.raises(ValueError):
            module.update(tmp_path, image)


def test_runtime_launcher_supports_external_mount(tmp_path):
    launcher = Path(__file__).resolve().parents[1] / "tools/run_native_worker.py"
    # Replace only the owned child launcher for this test; do not initialize a camera SDK.
    executable = tmp_path / "inspect-launch"
    executable.write_text(
        "#!/usr/bin/env python3\nimport json,sys;print(json.dumps(sys.argv[1:]))\n"
    )
    executable.chmod(0o700)
    env = {
        **os.environ,
        "V360_QEMU": str(executable),
        "V360_RUNTIME_ROOT": str(tmp_path / "runtime"),
        "V360_WORKER_PATH": str(tmp_path / "worker"),
    }
    result = subprocess.run(
        [sys.executable, str(launcher)], capture_output=True, text=True, env=env, check=True
    )
    args = json.loads(result.stdout)
    assert args[1] == str(tmp_path / "runtime/android-runtime-arm64")
    assert args[-1] == str(tmp_path / "worker")
    assert "android-libs/arm64-v8a" in args[-2]


def test_production_preflight_hash_check_never_connects(deployment, tmp_path, monkeypatch, capsys):
    _data, config, _snapshots, _key = deployment
    runtime = tmp_path / "runtime"
    hashes = {}
    for relative in (
        "native-worker",
        "android-runtime-arm64/linker64",
        "android-libs/arm64-v8a/libPPCS_API.so",
    ):
        path = runtime / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fake-runtime")
        hashes[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    linker = runtime / "android-runtime-arm64/system/bin"
    linker.mkdir(parents=True)
    (linker / "linker64").symlink_to("../../linker64")
    write_private_json(runtime / "manifest.json", {"sha256": hashes})
    monkeypatch.setenv("V360_RUNTIME_ROOT", str(runtime))
    monkeypatch.setattr(
        sys, "argv", ["production", "--config", str(config / "device.json"), "--check"]
    )
    assert production.main() == 0
    assert "not_attempted" in capsys.readouterr().out
    (runtime / "native-worker").write_bytes(b"changed")
    assert production.main() == 2
    assert "example-profile" not in capsys.readouterr().out


def test_backup_preserves_disk_floor(deployment, monkeypatch):
    from types import SimpleNamespace

    data, config, snapshots, _key = deployment
    monkeypatch.setattr(
        storage.shutil, "disk_usage", lambda _: SimpleNamespace(free=storage.FREE_RESERVE)
    )
    with pytest.raises(CameraError):
        backup(data, config, snapshots)
    assert not list(snapshots.iterdir())
