import hmac
import secrets
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from . import __version__, service
from .config import Settings
from .errors import CameraError
from .store import RecordingStore


class ListRequest(BaseModel):
    channel: int = Field(ge=0, le=255)
    start: datetime
    end: datetime


def create_app(root: Path, access_token: str, config_path: Path | None = None):
    if len(access_token) < 32:
        raise ValueError("Web access token must contain at least 32 characters")
    store = RecordingStore(root)
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="v360-camera")
    lock = threading.Lock()
    jobs = {}
    bearer = HTTPBearer(auto_error=False)

    @asynccontextmanager
    async def lifespan(app):
        yield
        pool.shutdown(wait=True, cancel_futures=True)

    app = FastAPI(title="V360 recording API", version=__version__, lifespan=lifespan)

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
    def status():
        configured = False
        transport = "not_configured"
        if config_path:
            try:
                settings = Settings.load(config_path)
                configured = True
                transport = settings.bridge_kind
            except CameraError:
                pass
        return {
            "camera_configured": configured,
            "camera_connection_state": "not_polled",
            "transport": transport,
            "hardware_acceptance": "not_recorded",
            "background_jobs": "memory_only; not resumed after restart",
        }

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

    @app.get("/api/jobs/{key}", dependencies=[Depends(authorize)])
    def get_job(key: str):
        if key not in jobs:
            raise HTTPException(404, "Job not found")
        return dict(jobs[key])

    return app
