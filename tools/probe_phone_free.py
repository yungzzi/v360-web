"""Bounded ARM64/Bionic experiments; never use ADB or send device login commands.

Requires project-local Ubuntu qemu-user and binutils-aarch64-linux-gnu packages
extracted under .tools/phone-free/sysroot, and privately acquired Android runtime
files under .private/android-runtime-arm64. This is not a production transport.
"""

import argparse
import json
import os
import re
import resource
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--initialize", action="store_true", help="Test init/deinit without a camera DID"
    )
    parser.add_argument(
        "--connect",
        action="store_true",
        help="Connect and close the configured camera; no device login",
    )
    parser.add_argument(
        "--build-worker", action="store_true", help="Build private stdio SDK worker"
    )
    args = parser.parse_args()
    do_initialize = args.initialize or args.connect
    runtime = ROOT / ".private/android-runtime-arm64"
    libraries = ROOT / ".private/android-libs/arm64-v8a"
    base = ROOT / ".tools/phone-free"
    sysroot = base / "sysroot"
    build = base / "build"
    build.mkdir(parents=True, exist_ok=True)
    assembler = sysroot / "usr/bin/aarch64-linux-gnu-as"
    linker = sysroot / "usr/bin/aarch64-linux-gnu-ld"
    qemu = sysroot / "usr/bin/qemu-aarch64"
    required = [assembler, linker, qemu, runtime / "linker64", libraries / "libPPCS_API.so"]
    if missing := [str(p.relative_to(ROOT)) for p in required if not p.is_file()]:
        print(json.dumps({"stage": "prerequisites", "missing": missing}))
        return 2
    loader = runtime / "system/bin/linker64"
    loader.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not loader.exists():
        loader.symlink_to("../../linker64")
    env = dict(os.environ)
    env["LD_LIBRARY_PATH"] = ":".join(
        str(sysroot / p) for p in ("usr/lib/x86_64-linux-gnu", "lib/x86_64-linux-gnu")
    )
    mode = (
        "native-worker"
        if args.build_worker
        else "connect"
        if args.connect
        else "initialize"
        if args.initialize
        else "version"
    )
    obj, exe = build / (mode + ".o"), build / (mode + "-probe")
    extra = ["--defsym", "INITIALIZE=1"] if do_initialize else []
    if args.connect:
        extra += ["--defsym", "CONNECT=1"]
    subprocess.run(
        [
            str(assembler),
            *extra,
            "-o",
            str(obj),
            str(
                ROOT
                / (
                    "tools/native_ppcs_worker.S"
                    if args.build_worker
                    else "tools/ppcs_version_arm64.S"
                )
            ),
        ],
        env=env,
        check=True,
        timeout=20,
    )
    subprocess.run(
        [
            str(linker),
            "-pie",
            "--allow-shlib-undefined",
            "-rpath-link",
            str(runtime),
            "-e",
            "_start",
            "--dynamic-linker",
            "/system/bin/linker64",
            "-L",
            str(runtime),
            "-L",
            str(libraries),
            "-o",
            str(exe),
            str(obj),
            "--no-as-needed",
            "-lPPCS_API",
            "-lc",
            "-lm",
            "-ldl",
            "-lstdc++",
            "-llog",
        ],
        env=env,
        check=True,
        timeout=20,
    )
    payload = b""
    if args.build_worker:
        print(json.dumps({"worker_built": str(exe.relative_to(ROOT))}))
        return 0
    if do_initialize:
        from v360_web.config import read_private_json

        profile = read_private_json(ROOT / ".private/sdk-profiles.json")
        payload = profile["initialize"].encode("utf-8")
        if not 4 <= len(payload) <= 4095 or b"\0" in payload:
            raise ValueError("Unsupported initialization parameter size")
        if args.connect:
            from v360_web.config import Settings

            cfg = Settings.load(ROOT / ".private/device.json")
            did = cfg.did.encode("ascii")
            server = (cfg.server_key or "").encode("ascii")
            if len(payload) > 2047 or len(did) > 127 or not 1 <= len(server) <= 1917:
                raise ValueError("Configured parameters exceed probe buffer sizes")
            buffer = bytearray(4095)
            buffer[: len(payload)] = payload
            buffer[2048 : 2048 + len(did)] = did
            buffer[2176 : 2176 + len(server)] = server
            buffer[4094] = cfg.connect_mode
            payload = bytes(buffer)
    result = subprocess.run(
        [
            str(qemu),
            "-L",
            str(runtime),
            "-E",
            "LD_LIBRARY_PATH=" + str(runtime) + ":" + str(libraries),
            str(exe),
        ],
        check=False,
        env=env,
        capture_output=True,
        input=payload,
        timeout=20,
        preexec_fn=lambda: resource.setrlimit(resource.RLIMIT_CORE, (0, 0)),
    )
    for name, value in (("stdout", result.stdout), ("stderr", result.stderr)):
        path = ROOT / (".private/phone-free-" + mode + "-" + name + ".log")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as output:
            output.write(value)
    text = result.stdout.decode("utf-8", "replace")
    report = {
        "stage": mode,
        "returncode": result.returncode,
        "stderr_bytes": len(result.stderr),
        "adb_used": False,
        "sdk_initialized": False,
        "camera_connected": False,
    }
    if result.returncode == 0 and do_initialize:
        pattern = r"(?m)^([0-9a-f]{8}) ([0-9a-f]{8})"
        pattern += r" ([0-9a-f]{8}) ([0-9a-f]{8})$" if args.connect else "$"
        matches = re.findall(pattern, text)
        if len(matches) != 1:
            raise ValueError("Initialization probe response missing; raw output retained privately")
        values = [int(value, 16) for value in matches[0]]
        values = [value - 2**32 if value >= 2**31 else value for value in values]
        report["initialize_ret"], report["deinitialize_ret"] = values[:2]
        report["sdk_initialized"] = report["initialize_ret"] == 0
        if args.connect:
            report["connect_ret"], report["force_close_ret"] = values[2:]
            report["camera_connected"] = report["connect_ret"] >= 0
            report["device_authentication"] = "not_attempted"
    elif result.returncode == 0:
        report["sdk_api_version"] = int(text.strip(), 16)
        report["matches_phone_reference"] = report["sdk_api_version"] == 84017664
    (base / (mode + "-probe.json")).write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    if result.returncode == 0 and do_initialize:
        success = report["initialize_ret"] == 0 and report["deinitialize_ret"] == 0
        if args.connect:
            success = success and report["camera_connected"] and report["force_close_ret"] == 0
        return 0 if success else 2
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
