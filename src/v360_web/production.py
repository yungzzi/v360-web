"""Single-process container entry point. Never expose local shutdown controls."""

import argparse
import hashlib
import json
import os
from pathlib import Path

import uvicorn

from .api import create_app
from .config import Settings, read_private_json
from .errors import CameraError
from .instance import InstanceLock


def main():
    parser = argparse.ArgumentParser(description="Production V360 service")
    parser.add_argument("--config", type=Path, default=Path("/app/config/device.json"))
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument(
        "--check", action="store_true", help="Validate files without connecting to camera"
    )
    args = parser.parse_args()
    os.umask(0o077)
    try:
        settings = Settings.load(args.config)
        token = read_private_json(args.config.parent / "web.json")["access_token"]
        if settings.bridge_kind != "native-arm64" or len(token) < 32:
            raise CameraError(
                "production_config", "Native transport and full web token are required."
            )
        runtime = Path(os.environ.get("V360_RUNTIME_ROOT", "/app/runtime"))
        expected = [
            runtime / "native-worker",
            runtime / "android-runtime-arm64/system/bin/linker64",
            runtime / "android-libs/arm64-v8a/libPPCS_API.so",
        ]
        if not all(p.is_file() for p in expected):
            raise CameraError(
                "runtime_missing", "Prepare the private runtime mount before startup."
            )
        manifest = read_private_json(runtime / "manifest.json")
        required = {
            "native-worker",
            "android-runtime-arm64/linker64",
            "android-libs/arm64-v8a/libPPCS_API.so",
        }
        if not required <= set(manifest["sha256"]):
            raise CameraError("runtime_integrity", "Runtime manifest is incomplete.")
        for relative, expected_hash in manifest["sha256"].items():
            item = runtime / relative
            if (
                not item.resolve().is_relative_to(runtime.resolve())
                or item.is_symlink()
                or hashlib.sha256(item.read_bytes()).hexdigest() != expected_hash
            ):
                raise CameraError("runtime_integrity", "Private runtime verification failed.")
        if args.check:
            print(json.dumps({"configuration": "valid", "camera_connection": "not_attempted"}))
            return 0
        with InstanceLock(settings.data_root):
            app = create_app(settings.data_root, token, args.config, start_backup=True)
            uvicorn.run(
                app,
                host="0.0.0.0",
                port=args.port,
                workers=1,
                access_log=False,
                ws_max_size=4096,
                proxy_headers=True,
                forwarded_allow_ips=os.environ.get("V360_TRUSTED_PROXIES", ""),
                timeout_graceful_shutdown=50,
            )
    except (CameraError, OSError, KeyError, ValueError):
        print(
            json.dumps(
                {
                    "error": "production_startup",
                    "message": "Check private mounts, permissions and service ownership.",
                }
            )
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
