import asyncio
import hmac
import ipaddress
import json
import queue
import secrets
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from . import __version__, battery, live, service
from .config import Settings
from .errors import CameraError
from .store import RecordingStore
from .streaming import StreamManager


class ListRequest(BaseModel):
    channel: int = Field(ge=0, le=255)
    start: datetime
    end: datetime


class LiveRequest(BaseModel):
    seconds: float = Field(default=10, ge=1, le=60, allow_inf_nan=False)
    resolution: int = Field(default=2, ge=0, le=3)


def create_app(
    root: Path, access_token: str, config_path: Path | None = None, *, shutdown_callback=None
):
    if len(access_token) < 32:
        raise ValueError("Web access token must contain at least 32 characters")
    store = RecordingStore(root)
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="v360-camera")
    lock = threading.Lock()
    streams = StreamManager(config_path, lock)
    jobs = {}
    bearer = HTTPBearer(auto_error=False)

    def local_controls(request: Request):
        try:
            return bool(shutdown_callback) and ipaddress.ip_address(request.client.host).is_loopback
        except (ValueError, AttributeError):
            return False

    @asynccontextmanager
    async def lifespan(app):
        yield
        await asyncio.to_thread(streams.close)
        pool.shutdown(wait=True, cancel_futures=True)

    app = FastAPI(title="V360 recording API", version=__version__, lifespan=lifespan)
    app.state.streams = streams
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

    def authorize(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]):
        if credentials is None or not hmac.compare_digest(
            credentials.credentials.encode(), access_token.encode()
        ):
            raise HTTPException(
                401, "Authentication required", headers={"WWW-Authenticate": "Bearer"}
            )

    def submit(function, *args):
        if config_path is None:
            raise HTTPException(503, "Camera profile has not been configured")
        if not lock.acquire(blocking=False):
            raise HTTPException(409, "A camera operation is already running")
        job_id = secrets.token_hex(16)
        jobs[job_id] = {"id": job_id, "state": "queued"}

        def run():
            jobs[job_id]["state"] = "running"
            try:
                jobs[job_id].update(state="complete", result=function(config_path, *args))
            except CameraError as error:
                jobs[job_id].update(state="failed", **error.as_dict())
            except Exception:  # noqa: BLE001 -- fail the task without exposing unexpected credential data
                jobs[job_id].update(state="failed", error="operation_failed")
            finally:
                lock.release()

        pool.submit(run)
        return {"id": job_id, "state": "queued"}

    @app.get("/health")
    def health():
        return {"service": "v360-web", "version": __version__}

    @app.get("/api/status", dependencies=[Depends(authorize)])
    def status(request: Request):
        configured = False
        battery_status = {"percent": None, "charging": None, "updated_at": None, "stale": True}
        transport = "not_configured"
        if config_path:
            try:
                settings = Settings.load(config_path)
                configured = True
                transport = settings.bridge_kind
                battery_status = battery.cached(settings)
            except CameraError:
                pass
        return {
            "camera_configured": configured,
            "camera_connection_state": "not_polled",
            "transport": transport,
            "hardware_acceptance": "not_recorded",
            "background_jobs": "memory_only; not resumed after restart",
            "live": streams.snapshot(),
            "battery": battery_status,
            "local_shutdown_available": local_controls(request),
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
            supplied = json.loads(raw).get("token", "")
            if not isinstance(supplied, str) or not hmac.compare_digest(
                supplied.encode(), access_token.encode()
            ):
                await socket.close(code=1008, reason="Authentication required")
                return
            viewer = streams.subscribe()
            await socket.send_json({"type": "state", "state": "starting"})

            async def send_frames():
                while not viewer.closed.is_set():
                    try:
                        event, value = await asyncio.to_thread(viewer.messages.get, True, 0.5)
                    except queue.Empty:
                        continue
                    if event == "init":
                        mime, data = value
                        await asyncio.wait_for(socket.send_json({"type": "init", "mime": mime}), 5)
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

    @app.get("/api/recordings/{key}/file", dependencies=[Depends(authorize)])
    def recording_file(key: str):
        path = store.media_path(key)
        if path is None:
            raise HTTPException(404, "Verified recording not found")
        return FileResponse(path, filename=f"{key}.media")

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

    @app.get("/api/live/captures/{key}/file", dependencies=[Depends(authorize)])
    def live_file(key: str):
        path = live.preview_path(root, key)
        if path is None:
            raise HTTPException(404, "Verified live capture not found")
        return FileResponse(path, media_type="video/mp4", filename=f"live-{key}.mp4")

    @app.get("/api/jobs/{key}", dependencies=[Depends(authorize)])
    def get_job(key: str):
        if key not in jobs:
            raise HTTPException(404, "Job not found")
        return dict(jobs[key])

    return app
