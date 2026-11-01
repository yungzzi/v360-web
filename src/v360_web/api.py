import asyncio
import hmac
import ipaddress
import json
import os
import queue
import secrets
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Annotated

from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Query,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from . import __version__, battery, live, maintenance, playback, progress, service, storage
from .backup import BackupManager
from .config import Settings
from .errors import CameraError
from .jobs import JobStore
from .lenses import views
from .sessions import COOKIE, REMEMBER_SECONDS, SessionStore
from .store import RecordingStore
from .streaming import StreamManager


class ListRequest(BaseModel):
    channel: int = Field(ge=0, le=255)
    start: datetime
    end: datetime


class LiveRequest(BaseModel):
    seconds: float = Field(default=10, ge=1, le=60, allow_inf_nan=False)
    resolution: int = Field(default=2, ge=0, le=3)


class MoveRequest(BaseModel):
    viewer_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    direction: str


class BackupRequest(BaseModel):
    enabled: bool
    mode: str = Field(pattern=r"^(local|server)$")
    interval_minutes: int = Field(default=10, ge=5, le=360)


class LoginRequest(BaseModel):
    remember: bool = False


class PinRequest(BaseModel):
    pinned: bool


def create_app(
    root: Path,
    access_token: str,
    config_path: Path | None = None,
    *,
    shutdown_callback=None,
    start_backup=False,
):
    if len(access_token) < 32:
        raise ValueError("Web access token must contain at least 32 characters")
    store = RecordingStore(root)
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="v360-camera")
    lock = threading.Lock()
    prefetch_guard = threading.Lock()
    prefetch_state = {"cancel": None, "waiters": 0}
    stopping = threading.Event()
    jobs = JobStore(root)
    jobs.recover_interrupted()
    backup = BackupManager(root, config_path, lock)

    def yield_background():
        with prefetch_guard:
            if prefetch_state["cancel"] is not None:
                prefetch_state["cancel"].set()
        backup.request_yield()

    streams = StreamManager(config_path, lock, before_subscribe=yield_background)
    media_sessions = {}
    sessions = SessionStore(root, access_token)
    bearer = HTTPBearer(auto_error=False)

    def local_controls(request: Request):
        try:
            return bool(shutdown_callback) and ipaddress.ip_address(request.client.host).is_loopback
        except (ValueError, AttributeError):
            return False

    @asynccontextmanager
    async def lifespan(app):
        if start_backup:
            backup.start()
        yield
        stopping.set()
        await asyncio.to_thread(backup.close)
        await asyncio.to_thread(streams.close)
        pool.shutdown(wait=True, cancel_futures=True)

    app = FastAPI(title="V360 recording API", version=__version__, lifespan=lifespan)
    app.state.streams = streams
    app.state.backup = backup
    static = Path(__file__).parent / "web"

    @app.middleware("http")
    async def private_headers(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; media-src 'self' blob:; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
        )
        return response

    @app.get("/", include_in_schema=False)
    def home():
        return FileResponse(static / "index.html", media_type="text/html")

    @app.get("/assets/{name}", include_in_schema=False)
    def asset(name: str):
        if name not in ("app.js", "style.css"):
            raise HTTPException(404)
        return FileResponse(static / name)

    def valid_token(value):
        return isinstance(value, str) and hmac.compare_digest(value.encode(), access_token.encode())

    def same_origin(request):
        if request.headers.get("sec-fetch-site") in ("cross-site", "same-site"):
            raise HTTPException(403, "Same-origin request required")
        origin = request.headers.get("origin")
        if origin and origin != f"{request.url.scheme}://{request.headers.get('host')}":
            raise HTTPException(403, "Same-origin request required")

    def authorize(
        request: Request,
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ):
        if credentials is not None:
            if valid_token(credentials.credentials):
                return
        else:
            same_origin(request)
            if sessions.valid(request.cookies.get(COOKIE, "")):
                return
        raise HTTPException(401, "Authentication required", headers={"WWW-Authenticate": "Bearer"})

    def cookie_path(request):
        return request.scope.get("root_path", "").rstrip("/") + "/api/"

    @app.post("/api/auth/login")
    def login(
        body: LoginRequest,
        request: Request,
        response: Response,
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ):
        same_origin(request)
        if credentials is None or not valid_token(credentials.credentials):
            raise HTTPException(401, "Authentication required")
        key = sessions.issue(body.remember, request.cookies.get(COOKIE, ""))
        if key is None:
            raise HTTPException(429, "Too many signed-in devices")
        response.set_cookie(
            COOKIE,
            key,
            max_age=REMEMBER_SECONDS if body.remember else None,
            httponly=True,
            samesite="strict",
            path=cookie_path(request),
            secure=request.url.scheme == "https" or os.environ.get("V360_SECURE_COOKIES") == "1",
        )
        return {"authenticated": True, "remembered": body.remember}

    @app.delete("/api/auth/session")
    def logout_session(request: Request, response: Response):
        same_origin(request)
        sessions.revoke(request.cookies.get(COOKIE, ""))
        media_sessions.pop(request.cookies.get("v360_media", ""), None)
        for name in (COOKIE, "v360_media"):
            response.delete_cookie(name, path=cookie_path(request))
        return {"closed": True}

    def authorize_media(
        request: Request,
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ):
        if credentials is not None:
            return authorize(request, credentials)
        if request.cookies.get(COOKIE):
            return authorize(request, None)
        session = request.cookies.get("v360_media", "")
        if request.headers.get("sec-fetch-site") in ("cross-site", "same-site"):
            raise HTTPException(403, "Same-origin playback required")
        if media_sessions.get(session, 0) <= time.monotonic():
            raise HTTPException(401, "Authentication required")

    @app.post("/api/playback/session", dependencies=[Depends(authorize)])
    def playback_session(request: Request, response: Response):
        if sessions.valid(request.cookies.get(COOKIE, "")):
            return {"ready": True}
        now = time.monotonic()
        for key in list(media_sessions):
            if media_sessions[key] <= now:
                del media_sessions[key]
        old = request.cookies.get("v360_media", "")
        media_sessions.pop(old, None)
        if len(media_sessions) >= 32:
            raise HTTPException(429, "Too many playback sessions")
        key = secrets.token_urlsafe(32)
        media_sessions[key] = now + 3600
        response.set_cookie(
            "v360_media",
            key,
            max_age=3600,
            httponly=True,
            samesite="strict",
            secure=request.url.scheme == "https" or os.environ.get("V360_SECURE_COOKIES") == "1",
            path=request.scope.get("root_path", "").rstrip("/") + "/api/",
        )
        return {"ready": True}

    @app.delete("/api/playback/session", dependencies=[Depends(authorize)])
    def end_playback_session(request: Request, response: Response):
        media_sessions.pop(request.cookies.get("v360_media", ""), None)
        response.delete_cookie(
            "v360_media", path=request.scope.get("root_path", "").rstrip("/") + "/api/"
        )
        return {"closed": True}

    def submit(function, *args, background=False):
        if config_path is None:
            raise HTTPException(503, "Camera profile has not been configured")
        if stopping.is_set():
            raise HTTPException(503, "Service is stopping")
        cancel = threading.Event() if background else None
        if background:
            with prefetch_guard:
                if prefetch_state["waiters"] or not lock.acquire(blocking=False):
                    raise HTTPException(409, "Foreground work has priority")
                prefetch_state["cancel"] = cancel
        else:
            with prefetch_guard:
                prefetch_state["waiters"] += 1
                if prefetch_state["cancel"] is not None:
                    prefetch_state["cancel"].set()
            backup.request_yield()
            try:
                acquired = lock.acquire(timeout=3)
            finally:
                with prefetch_guard:
                    prefetch_state["waiters"] -= 1
            if not acquired:
                raise HTTPException(409, "A camera operation is already running")
        job_id = secrets.token_hex(16)
        jobs.update_job(
            job_id, state="queued", kind="prefetch" if background else function.__name__
        )

        def run():
            jobs.update_job(job_id, state="running")
            cancellation = progress.shutdown_event.set(stopping)
            prefetch_hook = progress.cancel_event.set(cancel)
            hook = progress.callback.set(lambda value: jobs.update_job(job_id, progress=value))
            try:
                jobs.update_job(job_id, state="complete", result=function(config_path, *args))
            except CameraError as error:
                jobs.update_job(job_id, state="failed", **error.as_dict())
            except Exception:  # noqa: BLE001 -- fail the task without exposing unexpected credential data
                jobs.update_job(job_id, state="failed", error="operation_failed")
            finally:
                progress.cancel_event.reset(prefetch_hook)
                with prefetch_guard:
                    if background and prefetch_state["cancel"] is cancel:
                        prefetch_state["cancel"] = None
                progress.callback.reset(hook)
                progress.shutdown_event.reset(cancellation)
                lock.release()

        pool.submit(run)
        return {"id": job_id, "state": "queued"}

    @app.get("/health")
    def health():
        return {"service": "v360-web", "version": __version__}

    @app.get("/ready")
    def ready():
        try:
            with store.connection() as db:
                db.execute("SELECT id FROM recordings LIMIT 1").fetchone()
            scheduler = backup.snapshot()["scheduler_started"]
            healthy = not stopping.is_set() and (not start_backup or scheduler)
            if not healthy:
                raise HTTPException(503, "Service is not ready")
            return {
                "ready": True,
                "version": __version__,
                "revision": os.environ.get("V360_REVISION", "working-tree"),
                "scheduler_started": scheduler,
                "camera_checked": False,
            }
        except (OSError, sqlite3.Error) as error:
            raise HTTPException(503, "Data directory unavailable") from error

    @app.get("/api/status", dependencies=[Depends(authorize)])
    def status(request: Request):
        configured = False
        battery_status = {"percent": None, "charging": None, "updated_at": None, "stale": True}
        transport = "not_configured"
        available_views = views(None)
        if config_path:
            try:
                settings = Settings.load(config_path)
                configured = True
                transport = settings.bridge_kind
                available_views = views(settings)
                battery_status = battery.cached(settings)
            except CameraError:
                pass
        return {
            "camera_configured": configured,
            "camera_connection_state": "not_polled",
            "transport": transport,
            "hardware_acceptance": "not_recorded",
            "background_jobs": "persisted; interrupted jobs require retry",
            "live": streams.snapshot(),
            "lenses": available_views,
            "battery": battery_status,
            "local_shutdown_available": local_controls(request),
            "backup": backup.snapshot(),
        }

    @app.post("/api/local/shutdown", dependencies=[Depends(authorize)])
    def shutdown(request: Request):
        if not local_controls(request):
            raise HTTPException(403, "Local service shutdown is not available")
        origin = request.headers.get("origin")
        if origin and origin != str(request.base_url).rstrip("/"):
            raise HTTPException(403, "Same-origin request required")
        from fastapi.responses import JSONResponse

        return JSONResponse(
            {"state": "shutting_down"}, background=BackgroundTask(shutdown_callback)
        )

    @app.websocket("/api/live/ws")
    async def live_socket(socket: WebSocket):
        # Browsers authenticate in the first frame; no token in URL or logs.
        origin = socket.headers.get("origin")
        scheme = "https" if socket.url.scheme == "wss" else "http"
        if origin is not None and origin != f"{scheme}://{socket.headers.get('host')}":
            await socket.close(code=1008)
            return
        await socket.accept()
        viewer = None
        tasks = []
        try:
            raw = await asyncio.wait_for(socket.receive_text(), timeout=5)
            if len(raw) > 4096:
                raise ValueError
            message = json.loads(raw)
            supplied = message.get("token", "")
            lens = message.get("lens", 0)
            if type(lens) is not int or lens not in (0, 1, 2):
                raise ValueError
            browser_session = socket.cookies.get(COOKIE, "")
            cookie_auth = not valid_token(supplied)
            if cookie_auth and (origin is None or not sessions.valid(browser_session)):
                await socket.close(code=1008, reason="Authentication required")
                return
            for attempt in range(175):
                try:
                    viewer = streams.subscribe(lens)
                    break
                except CameraError as error:
                    if error.code == "lens_busy" and attempt < 10:
                        await asyncio.sleep(0.2)
                        continue
                    waiting = backup.running or streams.snapshot()["state"] == "stopping"
                    if error.code != "camera_busy" or not waiting or attempt == 174:
                        raise
                    await asyncio.sleep(0.2)
            await socket.send_json(
                {"type": "state", "state": "starting", "viewer_id": viewer.identity, "lens": lens}
            )

            async def send_frames():
                checked = 0
                while not viewer.closed.is_set():
                    if cookie_auth and time.monotonic() - checked >= 1:
                        if not await asyncio.to_thread(sessions.valid, browser_session):
                            await socket.close(code=1008, reason="Authentication required")
                            return
                        checked = time.monotonic()
                    try:
                        event, value = await asyncio.to_thread(viewer.messages.get, True, 0.5)
                    except queue.Empty:
                        continue
                    if event == "init":
                        mime, data = value
                        await asyncio.wait_for(
                            socket.send_json(
                                {
                                    "type": "init",
                                    "mime": mime,
                                    "audio": "mp4a.40.2" in mime,
                                    "ptz": lens == 0,
                                }
                            ),
                            5,
                        )
                        await asyncio.wait_for(socket.send_bytes(data), 5)
                    else:
                        await asyncio.wait_for(socket.send_bytes(value), 5)
                await socket.send_json({"type": "ended", "error": streams.snapshot()["error"]})

            async def receive_control():
                while True:
                    # Socket closure or browser stop releases this subscriber.
                    await socket.receive_text()

            tasks = [asyncio.create_task(send_frames()), asyncio.create_task(receive_control())]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except CameraError as error:
            await socket.send_json({"type": "error", "error": error.code})
        except (TimeoutError, ValueError, AttributeError, KeyError, TypeError):
            await socket.close(code=1008)
        except WebSocketDisconnect:
            pass
        except asyncio.CancelledError:
            pass
        finally:
            # Release ownership synchronously, even if the ASGI task is cancelled
            # again while awaiting child-task cleanup.
            if viewer is not None:
                streams.unsubscribe(viewer)
            for task in tasks:
                task.cancel()
            if tasks:
                try:
                    await asyncio.gather(*tasks, return_exceptions=True)
                except asyncio.CancelledError:
                    pass
            try:
                await socket.close()
            except (RuntimeError, WebSocketDisconnect, OSError, asyncio.CancelledError):
                pass

    @app.get("/api/recordings", dependencies=[Depends(authorize)])
    def recordings():
        return [
            {k: v for k, v in row.items() if k not in ["path", "device_id"]} for row in store.list()
        ]

    @app.get("/api/library", dependencies=[Depends(authorize)])
    def library(
        day: date | None = None,
        offset: int = Query(default=0, ge=0, le=1000000),
        limit: int = Query(default=30, ge=1, le=100),
    ):
        did = None
        if config_path:
            try:
                did = Settings.load(config_path).did
            except CameraError as error:
                raise HTTPException(503, "Camera profile unavailable") from error
        total, items = store.library(day, offset, limit, did)
        for row in items:
            row.pop("device_id", None)
            row.pop("path", None)
            row["original_ready"] = store.media_path(row["id"]) is not None
            row["playback_ready"] = playback.ready(root, row["id"]) is not None
        return {
            "items": items,
            "total": total,
            "offset": offset,
            "limit": limit,
            "storage": storage.summary(root),
            "catalog": store.catalog(did, day),
        }

    @app.get("/api/library/timeline", dependencies=[Depends(authorize)])
    def library_timeline(day: date):
        did = None
        if config_path:
            try:
                did = Settings.load(config_path).did
            except CameraError as error:
                raise HTTPException(503, "Camera profile unavailable") from error
        value = store.timeline(day, did)
        for item in value["items"]:
            item["playback_ready"] = (
                item["original_ready"] and playback.ready(root, item["id"]) is not None
            )
        value["catalog"] = store.catalog(did, day)
        return value

    @app.get("/api/recordings/{key}/file", dependencies=[Depends(authorize_media)])
    def recording_file(key: str):
        maintenance.protect(root, key)
        path = store.media_path(key)
        if path is None:
            raise HTTPException(404, "Verified recording not found")
        with path.open("rb") as file:
            suffix = ".mp4" if file.read(12)[4:8] == b"ftyp" else ".media"
        return maintenance.HeldFileResponse(path, root=root, key=key, filename=f"{key}{suffix}")

    @app.get("/api/recordings/{key}/preview", dependencies=[Depends(authorize_media)])
    def recording_preview(key: str):
        maintenance.protect(root, key)
        path = playback.ready(root, key)
        if path is None:
            raise HTTPException(404, "Prepared recording not found")
        return maintenance.HeldFileResponse(path, root=root, key=key, media_type="video/mp4")

    @app.get("/api/recordings/{key}/window", dependencies=[Depends(authorize_media)])
    def recording_window(key: str, start: int = Query(ge=0, lt=3600, multiple_of=30)):
        maintenance.protect(root, key)
        path = playback.ready(root, key, start)
        if path is None:
            raise HTTPException(404, "Prepared window not found")
        return maintenance.HeldFileResponse(path, root=root, key=key, media_type="video/mp4")

    @app.post("/api/recordings/{key}/lease", dependencies=[Depends(authorize)])
    def lease_recording(key: str):
        if store.get(key) is None:
            raise HTTPException(404, "Recording not found")
        maintenance.protect(root, key)
        return {"protected": True}

    @app.put("/api/recordings/{key}/pin", dependencies=[Depends(authorize)])
    def pin_recording(key: str, body: PinRequest):
        if not maintenance.pin(root, key, body.pinned):
            raise HTTPException(404, "Recording not found")
        return {"pinned": body.pinned}

    @app.get("/api/backup", dependencies=[Depends(authorize)])
    def backup_status():
        return backup.snapshot()

    @app.put("/api/backup", dependencies=[Depends(authorize)])
    def configure_backup(body: BackupRequest):
        return backup.configure(body.model_dump())

    @app.post("/api/backup/run", status_code=202, dependencies=[Depends(authorize)])
    def run_backup():
        try:
            backup.run_now()
        except CameraError as error:
            raise HTTPException(409, error.code) from error
        return backup.snapshot()

    @app.post("/api/jobs/playback/{key}", status_code=202, dependencies=[Depends(authorize)])
    def prepare_playback(
        key: str, start: int | None = Query(default=None, ge=0, lt=3600, multiple_of=30)
    ):
        return submit(playback.prepare, key, start)

    @app.post("/api/jobs/prefetch/{key}", status_code=202, dependencies=[Depends(authorize)])
    def prefetch_playback(key: str, start: int = Query(ge=0, lt=3600, multiple_of=30)):
        return submit(playback.prepare, key, start, background=True)

    @app.delete("/api/playback/prefetch", dependencies=[Depends(authorize)])
    def cancel_prefetch():
        with prefetch_guard:
            if prefetch_state["cancel"] is not None:
                prefetch_state["cancel"].set()
        return {"cancel_requested": True}

    @app.post("/api/jobs/list", status_code=202, dependencies=[Depends(authorize)])
    def list_job(body: ListRequest):
        if body.start.tzinfo is not None or body.end.tzinfo is not None or body.start > body.end:
            raise HTTPException(
                422, "Use an ordered range of device-local times without UTC offsets"
            )
        return submit(service.discover, body.channel, body.start, body.end)

    @app.post("/api/jobs/download/{key}", status_code=202, dependencies=[Depends(authorize)])
    def download_job(key: str):
        return submit(service.download, key)

    @app.post("/api/jobs/live", status_code=202, dependencies=[Depends(authorize)])
    def live_job(body: LiveRequest):
        return submit(live.capture, body.seconds, body.resolution)

    @app.post("/api/jobs/battery", status_code=202, dependencies=[Depends(authorize)])
    def battery_job():
        return submit(battery.refresh)

    @app.post("/api/live/ptz", dependencies=[Depends(authorize)])
    def move(body: MoveRequest):
        mapping = {"stop": 0, "up": 1, "down": 2, "left": 3, "right": 6}
        if body.direction not in mapping:
            raise HTTPException(422, "Unknown direction")
        try:
            streams.move(body.viewer_id, mapping[body.direction])
        except CameraError as error:
            raise HTTPException(409, error.code) from error
        return {"accepted": True}

    @app.get("/api/live/captures/{key}/file", dependencies=[Depends(authorize)])
    def live_file(key: str):
        path = live.preview_path(root, key)
        if path is None:
            raise HTTPException(404, "Verified live capture not found")
        return FileResponse(path, media_type="video/mp4", filename=f"live-{key}.mp4")

    @app.get("/api/jobs/{key}", dependencies=[Depends(authorize)])
    def get_job(key: str):
        value = jobs.get_job(key)
        if value is None:
            raise HTTPException(404, "Job not found")
        return value

    @app.get("/api/jobs", dependencies=[Depends(authorize)])
    def recent_jobs():
        return jobs.recent()

    return app
