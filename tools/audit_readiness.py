"""Read-only, bounded archive acceptance observer. Never opens a camera session."""

import argparse
import json
import sqlite3
import time
import urllib.request
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


def snapshot(home, verified_http=None):
    root = home / "data"
    verified_http = verified_http if verified_http is not None else set()
    now = datetime.now(ZoneInfo("Asia/Hong_Kong")).replace(tzinfo=None)
    with sqlite3.connect(f"file:{root / 'recordings.sqlite3'}?mode=ro", uri=True, timeout=5) as db:
        db.row_factory = sqlite3.Row
        policy = db.execute(
            "SELECT body FROM backup_state WHERE id LIKE 'recovery_floor:%' LIMIT 1"
        ).fetchone()
        floor = json.loads(policy[0]) if policy else (now - timedelta(days=3)).isoformat()
        rows = db.execute(
            "SELECT * FROM recordings WHERE start_local>=? AND state!='evicted'", (floor,)
        ).fetchall()
        checks = db.execute("SELECT diagnostics FROM catalog_checks").fetchall()
    token = json.loads((home / "config/web.json").read_text())["access_token"]
    details = []
    remaining_http = 20
    for row in rows:
        entry = {
            "id": row["id"],
            "start": row["start_local"],
            "state": row["state"],
            "error": row["error"],
            "ready": False,
        }
        end = datetime.fromisoformat(row["start_local"]) + timedelta(
            seconds=max(0, min(row["duration_raw"], 3600))
        )
        entry["deadline"] = (end + timedelta(minutes=30)).isoformat()
        entry["overdue"] = now > end + timedelta(minutes=30)
        try:
            manifest = root / "previews" / (row["id"] + ".json")
            info = json.loads(manifest.read_text())
            media = manifest.with_suffix(".mp4")
            source = (root / (row["path"] or "")).resolve()
            ready = (
                row["state"] == "verified"
                and info.get("verified")
                and source.is_relative_to(root.resolve())
                and source.is_file()
                and info.get("source_sha256") == row["sha256"]
                and media.is_file()
                and media.stat().st_size == info.get("bytes")
            )
            if ready:
                entry["ready"] = True
                stamp = row["web_ready_at"] if "web_ready_at" in set(row.keys()) else None
                ready_time = (
                    datetime.fromisoformat(stamp)
                    .astimezone(ZoneInfo("Asia/Hong_Kong"))
                    .replace(tzinfo=None)
                    if stamp
                    else datetime.fromtimestamp(
                        manifest.stat().st_mtime, ZoneInfo("Asia/Hong_Kong")
                    ).replace(tzinfo=None)
                )
                entry["ready_time"] = ready_time.isoformat()
                entry["timing_basis"] = "recorded" if stamp else "manifest_mtime"
                entry["latency_seconds"] = round((ready_time - end).total_seconds(), 1)
                cache = (row["id"], row["sha256"], manifest.stat().st_mtime_ns)
                if cache not in verified_http and remaining_http:
                    remaining_http -= 1
                    q = urllib.request.Request(
                        "http://127.0.0.1:8080/api/recordings/" + row["id"] + "/preview",
                        headers={"Authorization": "Bearer " + token, "Range": "bytes=0-1023"},
                    )
                    with urllib.request.urlopen(q, timeout=5) as response:
                        received = response.read(1024)
                        with media.open("rb") as f:
                            expected = f.read(1024)
                        if response.status == 206 and received == expected:
                            verified_http.add(cache)
                entry["http_verified"] = cache in verified_http
        except (OSError, ValueError, KeyError):
            entry["check_error"] = "file_or_http_check_failed"
        details.append(entry)
    overdue = [r for r in details if r["overdue"] and not r["ready"]]
    return {
        "at": datetime.now(UTC).isoformat(),
        "floor": floor,
        "known": len(details),
        "full_ready": sum(r["ready"] for r in details),
        "overdue_missing": len(overdue),
        "missing_reasons": dict(Counter(r["error"] or r["state"] for r in overdue)),
        "late_ready": sum(r["ready"] and r.get("latency_seconds", 0) > 1800 for r in details),
        "max_ready_latency_seconds": max(
            (r.get("latency_seconds", 0) for r in details if r["ready"]), default=None
        ),
        "oldest_missing": min((r["start"] for r in overdue), default=None),
        "http_verified": sum(r.get("http_verified", False) for r in details),
        "catalog_incomplete_checks": sum(
            not json.loads(r[0]).get("metadata_complete", False) for r in checks
        ),
        "acceptance": "pending_evidence_review",
        "records": details,
    }


def observe(home, cache):
    try:
        return snapshot(home, cache)
    except (sqlite3.Error, OSError, ValueError) as error:
        # An interrupted observation is not evidence of a complete archive.
        return {
            "at": datetime.now(UTC).isoformat(),
            "acceptance": "observation_failed",
            "observation_error": (
                "database_unavailable" if isinstance(error, sqlite3.Error)
                else "observation_unavailable"
            ),
        }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--watch-hours", type=float, default=0)
    args = parser.parse_args()
    home = args.home.resolve()
    output = home / "backups" / "readiness-observation"
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    cache = set()
    until = time.monotonic() + max(0, args.watch_hours) * 3600
    while True:
        report = observe(home, cache)
        temporary = output / "latest.new"
        temporary.write_text(json.dumps(report, indent=2) + "\n")
        temporary.chmod(0o600)
        temporary.replace(output / "latest.json")
        summary = {k: v for k, v in report.items() if k != "records"}
        with (output / "history.jsonl").open("a") as file:
            file.write(json.dumps(summary) + "\n")
        print(json.dumps(summary), flush=True)
        if time.monotonic() >= until:
            break
        time.sleep(min(60, max(0, until - time.monotonic())))


if __name__ == "__main__":
    main()
