"""Persistent, bounded automatic SD backup with cooperative viewing priority."""

import json
import threading
import time
from datetime import datetime, timedelta
from datetime import time as daytime
from zoneinfo import ZoneInfo

from . import playback, progress, service, storage
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
        self.ready_summary_until = 0
        self.ready_summary = {}
        self.recent_ready_count = 0
        state = self.db.read("status", {})
        self.state = {
            **state,
            "state": "waiting",
            "next_run": max(
                time.time() + 30,
                min(
                    state.get("next_run", 0),
                    time.time() + self.db.policy()["interval_minutes"] * 60,
                ),
            ),
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
        if time.monotonic() >= self.ready_summary_until:
            ready_dates = []
            with self.db.connection() as db:
                rows = db.execute(
                    "SELECT id,start_local,sha256,path FROM recordings WHERE device_id=? AND state='verified'",
                    (did,),
                ).fetchall()
            for row in rows:
                try:
                    manifest = self.root / "previews" / (row["id"] + ".json")
                    info = json.loads(manifest.read_text())
                    target = manifest.with_suffix(".mp4")
                    if (
                        info.get("verified")
                        and info.get("source_sha256") == row["sha256"]
                        and target.is_file()
                        and target.stat().st_size == info.get("bytes")
                        and row["path"]
                        and (self.root / row["path"]).is_file()
                    ):
                        ready_dates.append(row["start_local"])
                except (OSError, ValueError):
                    continue
            ready_dates.sort()
            cutoff = (
                datetime.now(ZoneInfo("Asia/Hong_Kong")).replace(tzinfo=None) - timedelta(days=3)
            ).isoformat()
            self.recent_ready_count = sum(day >= cutoff for day in ready_dates)
            self.ready_summary = {
                "ready": len(ready_dates),
                "oldest_ready": ready_dates[0] if ready_dates else None,
                "ready_span_days": round(
                    (
                        datetime.fromisoformat(ready_dates[-1])
                        - datetime.fromisoformat(ready_dates[0])
                    ).total_seconds()
                    / 86400,
                    1,
                )
                if ready_dates
                else 0,
            }
            self.ready_summary_until = time.monotonic() + 30
        with self.db.connection() as db:
            in_scope = db.execute(
                "SELECT count(*),coalesce(sum(state!='verified'),0) FROM recordings WHERE device_id=? AND state!='evicted' AND start_local>=?",
                (
                    did,
                    (
                        datetime.now(ZoneInfo("Asia/Hong_Kong")).replace(tzinfo=None)
                        - timedelta(days=3)
                    ).isoformat(),
                ),
            ).fetchone()
        return {
            **self.ready_summary,
            "awaiting_playback": max(0, in_scope[0] - self.recent_ready_count),
            "pending_in_scope": in_scope[1],
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
        if self.db.policy()["daily_bytes"] is None:
            record = self.db.get(key)
            if record and record["error"] == "backup_budget":
                return True
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
        cycle_started = time.monotonic()
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
            if policy["mode"] == "server" and time.monotonic() - cycle_started > 300:
                raise CameraError("backup_yielded", "Automatic work window finished.")
            if value["stage"] == "downloading" and not current_charged:
                total = value["total"]
                if (
                    policy["batch_bytes"] is not None and total > policy["batch_bytes"] - charged
                ) or (
                    policy["daily_bytes"] is not None
                    and total > policy["daily_bytes"] - accounting["bytes"]
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
        self.update(
            state="running",
            last_started=time.time(),
            error=None,
            last_file_error=None,
            last_prepare_error=None,
        )
        try:
            cleanup(self.root)
            storage.check_free(self.root)
            if policy["daily_bytes"] is not None and accounting["bytes"] >= policy["daily_bytes"]:
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
                    and row["state"] != "evicted"
                    and (
                        row["state"] != "verified"
                        or (policy["mode"] == "server" and not playback.ready(self.root, row["id"]))
                    )
                    and row["start_local"]
                    >= (end - timedelta(days=3 if policy["mode"] == "server" else 30)).isoformat()
                    and row["start_local"] <= end.isoformat()
                    and (
                        policy["mode"] != "server"
                        or datetime.fromisoformat(row["start_local"])
                        + timedelta(seconds=max(0, min(row["duration_raw"], 3600)))
                        <= end
                    )
                    and self.retry_ready(
                        ("prepare:" if row["state"] == "verified" else "") + row["id"], time.time()
                    )
                ]
                return sorted(rows, key=lambda row: row["start_local"], reverse=True)

            rows = candidates()
            # Only query one older day when the recent queue leaves room this round.
            # Cursor is persisted after a successful query; failures retry the same day.
            if policy["mode"] == "server" and not rows:
                progress.check_cancelled()
                cursor_key = "backfill3:" + settings.did
                cursor = self.db.read(cursor_key, {})
                offset = (
                    cursor.get("offset", 1) if cursor.get("anchor") == end.date().isoformat() else 1
                )
                today = end.date()
                day_to_query = today - timedelta(days=offset)
                start = datetime.combine(day_to_query, daytime.min)
                try:
                    historical = (
                        service.discover(
                            self.config, 0, start, start + timedelta(days=1) - timedelta(seconds=1)
                        )
                        if offset <= 2
                        else None
                    )
                except CameraError as error:
                    if error.code in ("backup_yielded", "device_authentication", "sdk_initialize"):
                        raise
                    self.update(backfill_error=error.code)
                else:
                    if historical is not None:
                        self.db.write(
                            cursor_key, {"offset": offset + 1, "anchor": today.isoformat()}
                        )
                    self.update(
                        backfill_day=day_to_query.isoformat()
                        if historical
                        else self.state.get("backfill_day"),
                        backfill_complete=historical["metadata_complete"]
                        if historical
                        else self.state.get("backfill_complete"),
                        backfill_horizon_days=3,
                        backfill_error=None,
                    )
                rows = candidates()
            attempts = 0
            prepared = 0
            for row in rows:
                progress.check_cancelled()
                if (
                    (policy["batch_files"] is not None and attempts >= policy["batch_files"])
                    or (policy["batch_bytes"] is not None and charged >= policy["batch_bytes"])
                    or (policy["mode"] == "server" and time.monotonic() - cycle_started > 300)
                ):
                    break
                if not self.retry_ready(
                    ("prepare:" if row["state"] == "verified" else "") + row["id"], time.time()
                ):
                    continue
                attempts += 1
                current_charged = False
                try:
                    if row["state"] != "verified":
                        service.download(self.config, row["id"])
                        downloaded += 1
                    if policy["mode"] == "server":
                        progress.check_cancelled()
                        playback.prepare(self.config, row["id"])
                        prepared += 1
                        self.ready_summary_until = 0
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
                    self.failed(
                        (
                            "prepare:"
                            if (self.db.get(row["id"]) or {}).get("state") == "verified"
                            else ""
                        )
                        + row["id"],
                        time.time(),
                    )
                    self.update(last_file_error=error.code)
                    if error.code == "backup_budget":
                        break
            self.update(last_prepared=prepared)
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
                next_run = max(time.time() + 30, next_run)
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
            self.update(
                last_saved=downloaded, last_prepared=prepared if "prepared" in locals() else 0
            )
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
