"""Bounded source-availability experiment through the authenticated job lock.

Run on deployment host after refreshing the catalog. It never changes backup
policy and never counts diagnostic transfers as archived recordings.
"""
import json
import sqlite3
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


def main():
    home = Path("/srv/v360-web")
    token = json.loads((home / "config/web.json").read_text())["access_token"]
    headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}

    def api(path, body=None):
        request = urllib.request.Request(
            "http://127.0.0.1:8080/api/" + path, headers=headers,
            data=json.dumps(body).encode() if body is not None else None,
        )
        return json.load(urllib.request.urlopen(request, timeout=15))

    with sqlite3.connect(f"file:{home}/data/recordings.sqlite3?mode=ro", uri=True) as db:
        row = db.execute("SELECT id,start_local,duration_raw FROM recordings ORDER BY start_local DESC LIMIT 1").fetchone()
    if not row:
        raise RuntimeError("No source sample")
    key, start, duration = row
    end = datetime.fromisoformat(start).replace(tzinfo=ZoneInfo("Asia/Hong_Kong")) + timedelta(seconds=max(0, min(duration, 3600)))
    output = home / "backups/source-lifetime-20261103.jsonl"
    for offset in (60, 300, 900, 1800):
        deadline = end.timestamp() + offset
        while time.time() < deadline:
            time.sleep(min(30, deadline - time.time()))
        result = {"start": start, "offset_seconds": offset, "observed_at": datetime.now(ZoneInfo("Asia/Hong_Kong")).isoformat()}
        try:
            if time.time() > deadline + 120:
                result["skipped"] = "measurement_window_already_passed"
            elif api("status")["live"]["viewers"]:
                result["skipped"] = "viewing_in_progress"
            else:
                job = api("jobs/download-probe/" + key + "?stop_first=true", {})
                for _ in range(130):
                    state = api("jobs/" + job["id"])
                    if state["state"] not in ("queued", "running"):
                        result.update(state=state["state"], result=state.get("result"), error=state.get("error"))
                        break
                    time.sleep(1)
                else:
                    result["error"] = "probe_not_finished"
        except (OSError, ValueError, KeyError) as error:
            result["error"] = type(error).__name__
        with output.open("a") as file:
            file.write(json.dumps(result) + "\n")
        output.chmod(0o600)
        print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
