"""Pull a versioned image, stop one service, back up metadata, replace and verify.

Run on the Docker host. On failure, stop the new service and retain recovery evidence;
never restore a database or run an old image against a changed schema automatically.
"""

import argparse
import fcntl
import json
import os
import re
import subprocess
import time
import urllib.request
from pathlib import Path


def execute(arguments):
    return subprocess.check_output(arguments, text=True, stderr=subprocess.STDOUT)


def set_image(home, image):
    path = home / ".env"
    rows = path.read_text().splitlines()
    rows = [row for row in rows if not row.startswith("V360_IMAGE=")]
    rows.append("V360_IMAGE=" + image)
    target = home / ".env.new"
    with target.open("x") as file:
        file.write("\n".join(rows) + "\n")
    target.chmod(0o600)
    target.replace(path)


def update(home, image, local=False):
    home = home.resolve()
    if not re.fullmatch(
        r"[a-z0-9][a-z0-9._/-]*(?::[A-Za-z0-9_.-]+|@sha256:[a-f0-9]{64})", image
    ) or image.endswith(":latest"):
        raise ValueError("Use an explicit release tag or digest")
    os.umask(0o077)
    with (home / ".deployment.lock").open("a") as guard:
        fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        compose = ["docker", "compose", "--project-directory", str(home)]
        service = json.loads(execute([*compose, "config", "--format", "json"]))["services"]["v360"]
        old = service["image"]
        mounts = {v["target"]: v for v in service["volumes"]}
        for name, target in (
            ("data", "/app/data"),
            ("config", "/app/config"),
            ("runtime", "/app/runtime"),
        ):
            mount = mounts[target]
            if mount["type"] != "bind" or Path(mount["source"]).resolve() != home / name:
                raise ValueError("Expected dedicated bind mounts under deployment home")
        ports = service["ports"]
        if len(ports) != 1 or ports[0].get("host_ip") != "127.0.0.1":
            raise ValueError("Only the loopback application port is allowed")
        if local:
            execute(["docker", "image", "inspect", image])
        else:
            execute(["docker", "pull", image])
        details = json.loads(execute(["docker", "image", "inspect", image]))[0]
        revision = details["Config"].get("Labels", {}).get("org.opencontainers.image.revision")
        if not revision or revision == "unknown":
            raise ValueError("Image needs a recorded source revision")
        backups = home / "backups"
        backups.mkdir(exist_ok=True, mode=0o700)
        state = {"old_image": old, "new_image": image, "revision": revision, "state": "stopping"}
        state_path = backups / ("release-" + str(time.time_ns()) + ".json")
        state_path.write_text(json.dumps(state, indent=2) + "\n")
        execute([*compose, "stop", "v360"])
        ids = execute([*compose, "ps", "-aq", "v360"]).split()
        if ids:
            stopped = json.loads(execute(["docker", "inspect", ids[0]]))[0]["State"]
            if stopped["Running"] or stopped["ExitCode"] == 137:
                raise ValueError("Previous service did not exit cleanly; inspect before replacing")
        backup_command = [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--cpus",
            "0.5",
            "--memory",
            "256m",
            "--memory-swap",
            "256m",
            "--user",
            service["user"],
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
        ]
        for name in ("data", "config", "backups"):
            backup_command += [
                "--mount",
                f"type=bind,source={home / name},target=/app/{name}"
                + (",readonly" if name != "backups" else ""),
            ]
        backup_command += [
            "--entrypoint",
            "python",
            old,
            "-m",
            "v360_web.operations",
            "backup",
            "--data",
            "/app/data",
            "--config",
            "/app/config",
            "--backups",
            "/app/backups",
        ]
        try:
            snapshot = json.loads(execute(backup_command))
        except (subprocess.CalledProcessError, ValueError):
            state["state"] = "backup_failed_old_service_stopped"
            state_path.write_text(json.dumps(state, indent=2) + "\n")
            raise
        state.update(snapshot=snapshot["backup"], state="starting")
        state_path.write_text(json.dumps(state, indent=2) + "\n")
        set_image(home, image)
        try:
            execute([*compose, "up", "-d", "--no-deps", "v360"])
            end = time.monotonic() + 90
            ready = None
            while time.monotonic() < end:
                try:
                    with urllib.request.urlopen(
                        "http://127.0.0.1:" + str(ports[0]["published"]) + "/ready", timeout=3
                    ) as response:
                        ready = json.load(response)
                    if (
                        ready.get("ready")
                        and ready.get("revision") == revision
                        and ready.get("scheduler_started")
                    ):
                        break
                except (OSError, ValueError):
                    pass
                time.sleep(1)
            else:
                raise ValueError("New release did not become ready")
        except BaseException:
            execute([*compose, "stop", "v360"])
            state["state"] = "failed_new_service_stopped"
            state_path.write_text(json.dumps(state, indent=2) + "\n")
            raise
        state["state"] = "ready"
        state_path.write_text(json.dumps(state, indent=2) + "\n")
        return {
            "state": "ready",
            "image": image,
            "revision": revision,
            "snapshot": snapshot["backup"],
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument(
        "--local-image", action="store_true", help="Local acceptance only: skip registry pull"
    )
    args = parser.parse_args()
    try:
        print(json.dumps(update(args.home, args.image, args.local_image)))
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError):
        print(
            json.dumps(
                {
                    "error": "release_failed",
                    "message": "Inspect the release record and service state; no automatic database rollback was performed.",
                }
            )
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
