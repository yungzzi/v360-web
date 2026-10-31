"""Persistent, bounded automatic SD backup with cooperative viewing priority."""

import threading
import time
from datetime import datetime, timedelta
from datetime import time as daytime
from zoneinfo import ZoneInfo

from . import progress, service, storage
from .backup_policy import DEFAULT, BackupPolicy
from .config import Settings
from .errors import CameraError
from .jobs import JobStore
from .maintenance import cleanup


class BackupManager:
    def __init__(self, root, config, camera_lock):
        self.root, self.config, self.camera_lock = root, config, camera_lock
        self.db = BackupPolicy(root)
        self.jobs = JobStore(root)
        self.guard = threading.RLock()
        self.stopping = threading.Event()
        self.wake = threading.Event()
        self.cancel = None
        self.thread = None
        self.running = False
        state = self.db.read("status", {})
        self.state = {
            **state,
            "state": "waiting",
            "next_run": max(time.time() + 30, state.get("next_run", 0)),
        }
        if state.get("state") == "authentication_required":
            self.state["state"] = "authentication_required"
        if self.db.read("policy") is None:
            policy = DEFAULT.copy()
            if storage.usage(root / "media") > 128 * 1024**2:
                policy["enabled"] = False
                policy["requires_profile"] = True
                self.state["state"] = "choose_profile"
            self.db.write("policy", policy)

    def start(self):
        if self.config and self.thread is None:
            self.thread = threading.Thread(target=self.run, name="v360-backup", daemon=True)
            self.thread.start()

    def archive_summary(self):
        """Counts describe indexed records, never a promise of SD completeness."""
        if not self.config:
            return {}
        try:
            did = Settings.load(self.config).did
        except CameraError:
            return {}
        with self.db.connection() as db:
            counts = dict(
                db.execute(
                    "SELECT state, COUNT(*) FROM recordings WHERE device_id=? GROUP BY state",
                    (did,),
                ).fetchall()
            )
            saved_bytes = db.execute(
                "SELECT COALESCE(SUM(bytes),0) FROM recordings WHERE device_id=? AND state='verified'",
                (did,),
            ).fetchone()[0]
        return {
            "saved": counts.get("verified", 0),
            "saved_bytes": saved_bytes,
            "pending": sum(v for k, v in counts.items() if k not in ("verified", "evicted")),
            "failed": counts.get("failed", 0),
            "evicted": counts.get("evicted", 0),
        }

    def snapshot(self):
        with self.guard:
            return {
                "policy": self.db.policy(),
                "archive": self.archive_summary(),
                **self.state,
                "running": self.running,
                "scheduler_started": bool(self.thread and self.thread.is_alive()),
            }

    def update(self, **values):
        with self.guard:
            self.state.update(values)
            self.db.write("status", self.state)

    def configure(self, values):
        with self.guard:
            policy = {k: v for k, v in self.db.policy().items() if k in DEFAULT}
            policy.update(values)
            self.db.write("policy", policy)
            if self.cancel:
                self.cancel.set()
            self.update(
                state="waiting" if policy["enabled"] else "paused",
                error=None,
                next_run=time.time() + 30,
            )
        self.start()
        self.wake.set()
        return self.snapshot()

    def request_yield(self):
        with self.guard:
            if self.running and self.cancel:
                self.cancel.set()

    def run_now(self):
        if self.running:
            raise CameraError("backup_running", "An automatic backup is already running.")
        if not self.db.policy()["enabled"]:
            raise CameraError("backup_paused", "Enable automatic backup first.")
        self.update(next_run=time.time())
        self.start()
        self.wake.set()

    def retry_ready(self, key, now):
        with self.db.connection() as db:
            row = db.execute("SELECT next_try FROM backup_retries WHERE id=?", (key,)).fetchone()
        return not row or row[0] <= now

    def failed(self, key, now):
        with self.db.connection() as db:
            row = db.execute("SELECT attempts FROM backup_retries WHERE id=?", (key,)).fetchone()
            attempts = (row[0] if row else 0) + 1
            delay = 3600 if attempts < 3 else 86400
            db.execute(
                "INSERT OR REPLACE INTO backup_retries VALUES (?,?,?)", (key, attempts, now + delay)
            )

    def cycle(self):
        if not self.config or not self.db.policy()["enabled"]:
            return
        if not self.camera_lock.acquire(blocking=False):
            self.update(state="waiting_for_camera", next_run=time.time() + 60)
            return
        with self.guard:
            self.running = True
            self.cancel = threading.Event()
            if self.stopping.is_set():
                self.cancel.set()
        cancellation = progress.cancel_event.set(self.cancel)
        hook = None
        downloaded = 0
        charged = 0
        job_id = "backup-" + str(time.time_ns())
        policy = self.db.policy()
        next_run = time.time() + policy["interval_minutes"] * 60
        day = datetime.now(ZoneInfo("Asia/Hong_Kong")).date().isoformat()
        accounting = self.db.read("daily", {"day": day, "bytes": 0})
        if accounting["day"] != day:
            accounting = {"day": day, "bytes": 0}
        current_charged = False

        def on_progress(value):
            nonlocal charged, current_charged
            if value["stage"] == "downloading" and not current_charged:
                total = value["total"]
                if (
                    total > policy["batch_bytes"] - charged
                    or total > policy["daily_bytes"] - accounting["bytes"]
                ):
                    raise CameraError("backup_budget", "Automatic transfer budget reached.")
                charged += total
                accounting["bytes"] += total
                self.db.write("daily", accounting)
                current_charged = True
            self.update(progress=value)
            self.jobs.update_job(job_id, progress=value)

        hook = progress.callback.set(on_progress)
        self.jobs.update_job(job_id, state="running", kind="automatic_backup")
        self.update(state="running", last_started=time.time(), error=None, last_file_error=None)
        try:
            cleanup(self.root)
            storage.check_free(self.root)
            if accounting["bytes"] >= policy["daily_bytes"]:
                self.update(state="daily_limit")
                self.jobs.update_job(
                    job_id, state="complete", result={"saved": 0, "reason": "daily_limit"}
                )
                return
            settings = Settings.load(self.config)
            end = datetime.now(ZoneInfo("Asia/Hong_Kong")).replace(tzinfo=None) - timedelta(
                seconds=60
            )
            result = service.discover(self.config, 0, end - timedelta(hours=24), end)
            self.update(
                metadata_complete=result["metadata_complete"], queried_records=result["records"]
            )

            def candidates():
                rows = [
                    row
                    for row in self.db.list()
                    if row["device_id"] == settings.did
                    and row["channel"] == 0
                    and row["state"] not in ("verified", "evicted")
                    and row["start_local"] >= (end - timedelta(days=30)).isoformat()
                    and row["start_local"] <= end.isoformat()
                    and self.retry_ready(row["id"], time.time())
                ]
                return sorted(rows, key=lambda row: row["start_local"], reverse=True)

            rows = candidates()
            # Only query one older day when the recent queue leaves room this round.
            # Cursor is persisted after a successful query; failures retry the same day.
            if policy["mode"] == "server" and len(rows) < policy["batch_files"]:
                progress.check_cancelled()
                cursor_key = "backfill:" + settings.did
                cursor = self.db.read(cursor_key, {})
                offset = cursor.get("offset", 1)
                today = end.date()
                day_to_query = today - timedelta(days=offset)
                start = datetime.combine(day_to_query, daytime.min)
                try:
                    historical = service.discover(
                        self.config, 0, start, start + timedelta(days=1) - timedelta(seconds=1)
                    )
                except CameraError as error:
                    if error.code in ("backup_yielded", "device_authentication", "sdk_initialize"):
                        raise
                    self.update(backfill_error=error.code)
                else:
                    self.db.write(cursor_key, {"offset": offset % 29 + 1})
                    self.update(
                        backfill_day=day_to_query.isoformat(),
                        backfill_complete=historical["metadata_complete"],
                        backfill_horizon_days=30,
                        backfill_error=None,
                    )
                rows = candidates()
            attempts = 0
            for row in rows:
                progress.check_cancelled()
                if attempts >= policy["batch_files"] or charged >= policy["batch_bytes"]:
                    break
                if not self.retry_ready(row["id"], time.time()):
                    continue
                attempts += 1
                current_charged = False
                try:
                    service.download(self.config, row["id"])
                    downloaded += 1
                    with self.db.connection() as db:
                        db.execute("DELETE FROM backup_retries WHERE id=?", (row["id"],))
                except CameraError as error:
                    if error.code in (
                        "backup_yielded",
                        "device_authentication",
                        "sdk_initialize",
                        "disk_reserve",
                        "storage_limit",
                    ):
                        raise
                    self.failed(row["id"], time.time())
                    self.update(last_file_error=error.code)
                    if error.code == "backup_budget":
                        break
            if (
                policy["mode"] == "server"
                and downloaded > 0
                and candidates()
                and accounting["bytes"] < policy["daily_bytes"]
            ):
                next_run = min(next_run, time.time() + 600)
            self.update(
                state="waiting",
                last_finished=time.time(),
                last_saved=downloaded,
                last_transfer_budget=charged,
                daily_bytes=accounting["bytes"],
            )
            self.jobs.update_job(
                job_id, state="complete", result={"saved": downloaded, "charged_bytes": charged}
            )
        except CameraError as error:
            state = "waiting" if error.code == "backup_yielded" else "failed"
            if error.code in ("device_authentication", "sdk_initialize"):
                state = "authentication_required"
            self.update(state=state, error=error.code)
            self.jobs.update_job(job_id, state="failed", **error.as_dict())
            if error.code == "backup_yielded":
                next_run = time.time() + 300
        except Exception:  # noqa: BLE001 - Keep the background scheduler alive after unexpected failures.
            self.update(state="failed", error="backup_failed")
            self.jobs.update_job(job_id, state="failed", error="backup_failed")
        finally:
            progress.callback.reset(hook)
            progress.cancel_event.reset(cancellation)
            with self.guard:
                self.running = False
                self.cancel = None
                self.update(next_run=next_run)
            self.camera_lock.release()

    def run(self):
        while not self.stopping.is_set():
            state = self.snapshot()
            if (
                state["policy"]["enabled"]
                and state["state"] != "authentication_required"
                and time.time() >= state["next_run"]
            ):
                self.cycle()
            delay = max(1, min(60, self.state["next_run"] - time.time()))
            if not state["policy"]["enabled"] or state["state"] == "authentication_required":
                delay = 60
            self.wake.wait(timeout=delay)
            self.wake.clear()

    def close(self):
        self.stopping.set()
        self.request_yield()
        self.wake.set()
        if self.thread:
            self.thread.join(timeout=45)
