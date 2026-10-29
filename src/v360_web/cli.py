from __future__ import annotations

import argparse
import getpass
import json
import platform
import shutil
import sys
from datetime import datetime
from pathlib import Path

from . import __version__, service
from .apk import import_apk, import_cloud_profile
from .bridge import create_bridge
from .config import Settings, initialize_config, read_private_json, write_private_json
from .errors import CameraError
from .media import find_tool, probe
from .store import RecordingStore


def output(value):
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def main():
    parser = argparse.ArgumentParser(
        description="V360 private camera client (hardware acceptance pending)"
    )
    parser.add_argument("--config", type=Path, default=Path(".private/device.json"))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor")
    commands.add_parser("init-config")
    commands.add_parser("configure")
    commands.add_parser("configure-bridge")
    commands.add_parser("pair-bridge")
    commands.add_parser("bridge-check")
    commands.add_parser("connect-check")
    commands.add_parser("cloud-login")
    cloud_resume = commands.add_parser("cloud-import")
    cloud_resume.add_argument("--cached", action="store_true")
    cloud_import = commands.add_parser("import-cloud-apk")
    cloud_import.add_argument("apk", type=Path)
    imported = commands.add_parser("import-apk")
    imported.add_argument("apk", type=Path)
    listing = commands.add_parser("list")
    listing.add_argument("--channel", type=int, required=True)
    listing.add_argument("--start", type=datetime.fromisoformat, required=True)
    listing.add_argument("--end", type=datetime.fromisoformat, required=True)
    download = commands.add_parser("download")
    download.add_argument("recording_id")
    live = commands.add_parser("live-capture", help="Capture a bounded video-only live preview")
    live.add_argument("--seconds", type=float, default=10)
    live.add_argument("--resolution", type=int, choices=range(4), default=2)
    commands.add_parser("recordings")
    inspect = commands.add_parser("probe")
    inspect.add_argument("file", type=Path)
    serving = commands.add_parser("serve")
    serving.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    try:
        if args.command == "doctor":
            output(
                {
                    "version": __version__,
                    "platform": platform.system(),
                    "machine": platform.machine(),
                    "python": platform.python_version(),
                    "ffmpeg": find_tool("ffmpeg"),
                    "ffprobe": find_tool("ffprobe"),
                    "device_config_exists": args.config.is_file(),
                    "hardware_acceptance": "not_performed",
                    "camera_connection": "not_attempted",
                }
            )
        elif args.command == "init-config":
            initialize_config(args.config.parent)
            output({"created": [str(args.config.parent / n) for n in ["device.json", "web.json"]]})
        elif args.command == "import-apk":
            output(import_apk(args.apk, args.config.parent))
        elif args.command == "import-cloud-apk":
            output(import_cloud_profile(args.apk, args.config.parent))
        elif args.command == "cloud-import":
            from .cloud import resume_import

            resume_import(args.config, cached=args.cached)
            output({"device_credentials_imported": True, "camera_connection": "not_attempted"})
        elif args.command == "cloud-login":
            from .cloud import onboard

            account = input("V360 Pro account (phone/email; not stored): ").strip()
            password = getpass.getpass("V360 Pro account password (not stored): ")
            onboard(
                args.config,
                account,
                password,
                progress=lambda s: print(s, file=sys.stderr, flush=True),
            )
            output({"device_credentials_imported": True, "camera_connection": "not_attempted"})
        elif args.command in ("configure", "configure-bridge"):
            cfg = read_private_json(args.config)
            if args.command == "configure":
                cfg["device"]["did"] = input("Camera DID (stored locally): ").strip()
                cfg["device"]["password"] = getpass.getpass("Normal device password: ")
                cfg["device"]["user_id"] = int(
                    input("Device command user ID (0 only when applicable): ")
                )
                cfg["device"]["shared"] = input("Shared device? [y/N]: ").lower() == "y"
            if cfg["bridge"].get("adapter") != "native-arm64" and (
                args.command == "configure-bridge" or not cfg["bridge"].get("token")
            ):
                cfg["bridge"]["token"] = getpass.getpass("Private token copied from helper: ")
            if (
                platform.system() == "Linux"
                and shutil.which("python.exe")
                and cfg["bridge"].get("adapter", "loopback") in ("loopback", "windows-usb")
            ):
                cfg["bridge"].update(
                    adapter="windows-usb", windows_python=shutil.which("python.exe")
                )
            temporary = args.config.with_suffix(".new")
            write_private_json(temporary, cfg)
            temporary.replace(args.config)
            output({"configured": True, "camera_connection": "not_attempted"})
        elif args.command == "pair-bridge":
            cfg = read_private_json(args.config)
            bridge_cfg = cfg["bridge"]
            if platform.system() == "Linux" and shutil.which("python.exe"):
                bridge_cfg.update(adapter="windows-usb", windows_python=shutil.which("python.exe"))
            bridge = create_bridge(
                bridge_cfg["host"],
                bridge_cfg["port"],
                "pairing-placeholder-not-a-secret-1234567890",
                bridge_cfg.get("adapter", "loopback"),
                bridge_cfg.get("windows_python", ""),
            )
            try:
                bridge.open()
                response = bridge.rpc("pair", code=getpass.getpass("Six-digit code on phone: "))
                bridge_cfg["token"] = response["token"]
            finally:
                bridge.close()
            temporary = args.config.with_suffix(".new")
            write_private_json(temporary, cfg)
            temporary.replace(args.config)
            output({"paired": True, "camera_connection": "not_attempted"})
        elif args.command == "bridge-check":
            cfg = read_private_json(args.config)["bridge"]
            bridge = create_bridge(
                cfg["host"],
                cfg["port"],
                cfg.get("token", ""),
                cfg.get("adapter", "loopback"),
                cfg.get("windows_python", ""),
            )
            try:
                bridge.open()
                output(bridge.rpc("status"))
            finally:
                bridge.close()
        elif args.command == "connect-check":
            from .client import CameraClient

            with CameraClient(Settings.load(args.config)):
                output({"device_authentication": "accepted", "video_retrieval": "not_attempted"})
        elif args.command == "list":
            output(service.discover(args.config, args.channel, args.start, args.end))
        elif args.command == "download":
            output(service.download(args.config, args.recording_id))
        elif args.command == "live-capture":
            from .live import capture

            output(capture(args.config, args.seconds, args.resolution))
        elif args.command == "recordings":
            settings = Settings.load(args.config)
            output(
                [
                    {k: v for k, v in row.items() if k not in ["device_id", "path"]}
                    for row in RecordingStore(settings.data_root).list()
                ]
            )
        elif args.command == "probe":
            output(probe(args.file))
        elif args.command == "serve":
            import uvicorn

            from .api import create_app

            cfg = read_private_json(args.config) if args.config.exists() else {}
            root = (args.config.parent / cfg.get("data_root", "../data")).resolve()
            token = read_private_json(args.config.parent / "web.json")["access_token"]
            app = create_app(root, token, args.config)
            uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False, ws_max_size=4096)
    except (EOFError, KeyboardInterrupt):
        output({"error": "cancelled", "message": "Local operation cancelled."})
        return 130
    except CameraError as error:
        output(error.as_dict())
        return 2
    except (OSError, ValueError, KeyError):
        output(
            {
                "error": "local_input",
                "message": "Local input or files are invalid; credentials were not printed.",
            }
        )
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
