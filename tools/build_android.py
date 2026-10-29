"""Build and sign the USB helper locally, using only the user's imported SDK."""

import hashlib
import json
import os
import secrets
import subprocess
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / ".tools"
PRIVATE = ROOT / ".private"


def unique(name):
    candidates = list((TOOLS / "android").rglob(name))
    if len(candidates) != 1:
        raise RuntimeError(f"Expected one project-local Android tool: {name}")
    return candidates[0]


def main():
    java_root = TOOLS / "sysroot/usr/lib/jvm/java-17-openjdk-amd64"
    java, javac, jar, keytool = [java_root / "bin" / n for n in ["java", "javac", "jar", "keytool"]]
    android = unique("android.jar")
    d8 = unique("d8.jar")
    aapt, align, signer = unique("aapt2"), unique("zipalign"), unique("apksigner.jar")
    build = TOOLS / "android-build"
    classes, dex = build / "classes", build / "dex"
    classes.mkdir(parents=True, exist_ok=True)
    dex.mkdir(exist_ok=True)
    sources = sorted((ROOT / "android/src").rglob("*.java"))
    subprocess.run(
        [str(javac), "--release", "8", "-cp", str(android), "-d", str(classes), *map(str, sources)],
        check=True,
    )
    class_jar = build / "classes.jar"
    subprocess.run([str(jar), "cf", str(class_jar), "-C", str(classes), "."], check=True)
    subprocess.run(
        [
            str(java),
            "-cp",
            str(d8),
            "com.android.tools.r8.D8",
            "--lib",
            str(android),
            "--min-api",
            "26",
            "--output",
            str(dex),
            str(class_jar),
        ],
        check=True,
    )
    unsigned = build / "unsigned.apk"
    subprocess.run(
        [
            str(aapt),
            "link",
            "-I",
            str(android),
            "--manifest",
            str(ROOT / "android/AndroidManifest.xml"),
            "--min-sdk-version",
            "26",
            "--target-sdk-version",
            "34",
            "--version-code",
            "1",
            "--version-name",
            "0.1.0",
            "-o",
            str(unsigned),
        ],
        check=True,
    )
    library_evidence = []
    with zipfile.ZipFile(unsigned, "a") as archive:
        archive.write(dex / "classes.dex", "classes.dex")
        for abi in ["arm64-v8a", "armeabi-v7a"]:
            lib = PRIVATE / "android-libs" / abi / "libPPCS_API.so"
            archive.write(lib, f"lib/{abi}/libPPCS_API.so", compress_type=zipfile.ZIP_STORED)
            library_evidence.append(
                {"abi": abi, "sha256": hashlib.sha256(lib.read_bytes()).hexdigest()}
            )
    aligned = build / "aligned.apk"
    subprocess.run([str(align), "-f", "-P", "16", "4", str(unsigned), str(aligned)], check=True)
    password_file = PRIVATE / "signing-password"
    if not password_file.exists():
        fd = os.open(password_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_urlsafe(32))
    env = dict(os.environ)
    env["V360_DEV_KEY_PASSWORD"] = password_file.read_text()
    keystore = PRIVATE / "bridge-signing.p12"
    if not keystore.exists():
        subprocess.run(
            [
                str(keytool),
                "-genkeypair",
                "-keystore",
                str(keystore),
                "-storepass:env",
                "V360_DEV_KEY_PASSWORD",
                "-keypass:env",
                "V360_DEV_KEY_PASSWORD",
                "-alias",
                "bridge",
                "-keyalg",
                "RSA",
                "-keysize",
                "3072",
                "-validity",
                "3650",
                "-dname",
                "CN=V360 Web Local Development",
                "-noprompt",
            ],
            env=env,
            check=True,
        )
        keystore.chmod(0o600)
    output = ROOT / "dist/v360-usb-bridge-0.1.0.apk"
    output.parent.mkdir(exist_ok=True)
    subprocess.run(
        [
            str(java),
            "-jar",
            str(signer),
            "sign",
            "--ks",
            str(keystore),
            "--ks-key-alias",
            "bridge",
            "--ks-pass",
            "env:V360_DEV_KEY_PASSWORD",
            "--key-pass",
            "env:V360_DEV_KEY_PASSWORD",
            "--v4-signing-enabled",
            "false",
            "--out",
            str(output),
            str(aligned),
        ],
        env=env,
        check=True,
    )
    verified = subprocess.run(
        [str(java), "-jar", str(signer), "verify", "--verbose", str(output)],
        text=True,
        capture_output=True,
        check=True,
    )
    subprocess.run([str(align), "-c", "-P", "16", "4", str(output)], check=True)
    evidence = {
        "apk": output.name,
        "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "bytes": output.stat().st_size,
        "signature_verification": verified.stdout,
        "sdk_libraries": library_evidence,
        "java_sources": {
            str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources
        },
        "device_installation": "not performed",
        "camera_acceptance": "not performed",
    }
    (output.parent / "android-build-evidence.json").write_text(json.dumps(evidence, indent=2))
    print(json.dumps({k: evidence[k] for k in ["apk", "sha256", "bytes"]}))


if __name__ == "__main__":
    main()
