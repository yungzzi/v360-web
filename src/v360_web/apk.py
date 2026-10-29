"""Read the user's known APK without executing its code or publishing its SDK."""

from __future__ import annotations

import hashlib
import struct
import zipfile
from pathlib import Path

from .config import write_private_json
from .errors import CameraError

KNOWN_APK = "f7f05455d9c6e881eb9584b0b6eaeba5f99feef381a3d27415d6051f6e5c43e0"


class DexFile:
    def __init__(self, data: bytes):
        self.data = data
        self.strings = []
        for i in range(self.u32(56)):
            offset = self.u32(self.u32(60) + i * 4)
            _, start = self.uleb(offset)
            self.strings.append(data[start : data.index(0, start)].decode("utf-8", "replace"))
        self.types = [self.strings[self.u32(self.u32(68) + i * 4)] for i in range(self.u32(64))]
        self.fields = []
        for i in range(self.u32(80)):
            at = self.u32(84) + i * 8
            self.fields.append((self.types[self.u16(at)], self.strings[self.u32(at + 4)]))
        self.methods = []
        for i in range(self.u32(88)):
            at = self.u32(92) + i * 8
            self.methods.append((self.types[self.u16(at)], self.strings[self.u32(at + 4)]))
        self.classes = {
            self.types[self.u32(self.u32(100) + i * 32)]: self.u32(100) + i * 32
            for i in range(self.u32(96))
        }

    def u16(self, at):
        return struct.unpack_from("<H", self.data, at)[0]

    def u32(self, at):
        return struct.unpack_from("<I", self.data, at)[0]

    def uleb(self, at):
        value = shift = 0
        while True:
            byte = self.data[at]
            at += 1
            value |= (byte & 127) << shift
            if byte < 128:
                return value, at
            shift += 7

    def members(self, name):
        at = self.u32(self.classes[name] + 24)
        counts = []
        for _ in range(4):
            count, at = self.uleb(at)
            counts.append(count)
        fields, methods = [], []
        for group, count in enumerate(counts):
            index = 0
            for _ in range(count):
                delta, at = self.uleb(at)
                index += delta
                flags, at = self.uleb(at)
                if group < 2:
                    fields.append((group, index, flags))
                else:
                    code, at = self.uleb(at)
                    methods.append((index, code))
        return fields, methods

    def static_values(self, name):
        fields, _ = self.members(name)
        names = [self.fields[index][1] for group, index, _ in fields if group == 0]
        at = self.u32(self.classes[name] + 28)
        if not at:
            return {}
        count, at = self.uleb(at)
        values = {}
        for key in names[:count]:
            tag = self.data[at]
            at += 1
            kind, length = tag & 31, (tag >> 5) + 1
            if kind == 0x1E:
                value = None
            elif kind == 0x1F:
                value = tag >> 5
            else:
                raw = self.data[at : at + length]
                at += length
                value = int.from_bytes(raw, "little", signed=kind in (0, 2, 4, 6))
                if kind == 0x17:
                    value = self.strings[value]
            values[key] = value
        return values

    def code(self, name, method):
        _, methods = self.members(name)
        for index, code in methods:
            if self.methods[index][1] == method and code:
                size = self.u32(code + 12)
                yield struct.unpack_from(f"<{size}H", self.data, code + 16)

    def returned_string(self, name, method):
        for words in self.code(name, method):
            if words[0] & 255 == 0x1A:
                return self.strings[words[1]]
            if words[0] & 255 == 0x1B:
                return self.strings[words[1] | words[2] << 16]
        raise CameraError("apk_layout", "SDK initialization layout is not recognized.")

    def assigned_integer(self, name, field):
        # The known APK uses adjacent const/16 or const/4 + iput for these defaults.
        for words in self.code(name, "<init>"):
            for pos in range(len(words) - 1):
                opcode = words[pos] & 255
                if opcode not in (0x59, 0x5C, 0x5D, 0x5E, 0x5F):
                    continue
                index = words[pos + 1]
                if index >= len(self.fields) or self.fields[index][1] != field:
                    continue
                register = (words[pos] >> 8) & 15
                if pos >= 2 and words[pos - 2] & 255 == 0x13 and words[pos - 2] >> 8 == register:
                    return struct.unpack("<h", struct.pack("<H", words[pos - 1]))[0]
                if (
                    pos >= 1
                    and words[pos - 1] & 255 == 0x12
                    and (words[pos - 1] >> 8) & 15 == register
                ):
                    value = words[pos - 1] >> 12
                    return value - 16 if value & 8 else value
        raise CameraError("apk_layout", f"Known default could not be confirmed: {field}")


def import_apk(apk: Path, private: Path):
    digest = hashlib.sha256()
    with apk.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != KNOWN_APK:
        raise CameraError(
            "unsupported_apk", "This build only supports the analyzed V360 Pro 7.2.0 APK."
        )
    with zipfile.ZipFile(apk) as archive:
        dex = DexFile(archive.read("classes4.dex"))
        sdk = dex.static_values("Lppcs/sdk/P2pSDK;")
        command = dex.static_values("Lppcs/sdk/connect/channel/CmdChannel;")
        facts = {
            "command_magic": command.get("DataHand"),
            "command_channel": command.get("CH", 0),
            "download_channel": dex.assigned_integer(
                "Lppcs/sdk/connect/channel/DownLoadChannel;", "CH"
            ),
        }
        if facts["command_magic"] != 0x76768888 or facts["download_channel"] != 4:
            raise CameraError("apk_layout", "Protocol constants do not match the analyzed layout.")
        profiles = {
            key: value
            for key, value in sdk.items()
            if key.startswith("NEW_INIT") and isinstance(value, str)
        }
        if len(profiles) != 4:
            raise CameraError("apk_layout", "SDK connection profiles are incomplete.")
        value = {
            "source_apk_sha256": KNOWN_APK,
            "protocol_facts": facts,
            "initialize": dex.returned_string("Lppcs/sdk/P2pSDK;", "createInitParams"),
            "server_profiles": profiles,
        }
        write_private_json(private / "sdk-profiles.json", value)
        libraries = []
        for abi in ["arm64-v8a", "armeabi-v7a"]:
            blob = archive.read(f"lib/{abi}/libPPCS_API.so")
            target = private / "android-libs" / abi / "libPPCS_API.so"
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with target.open("xb") as destination:
                destination.write(blob)
            target.chmod(0o600)
            libraries.append({"abi": abi, "sha256": hashlib.sha256(blob).hexdigest()})
        write_private_json(
            private / "sdk-provenance.json",
            {"apk_sha256": KNOWN_APK, "libraries": libraries, "target_executed": False},
        )
    return {"apk_sha256": KNOWN_APK, "profile_count": len(profiles), "facts": facts}


def import_cloud_profile(apk: Path, private: Path):
    """Import the shipped account cipher key separately; never overwrite SDK credentials."""
    digest = hashlib.sha256()
    with apk.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != KNOWN_APK:
        raise CameraError("unsupported_apk", "Use the analyzed V360 Pro 7.2.0 APK.")
    with zipfile.ZipFile(apk) as archive:
        dex = DexFile(archive.read("classes10.dex"))
        key = dex.static_values("Lcom/qianniao/base/utils/AesUtil;").get("WEB_KEY")
    if not isinstance(key, str) or len(key.encode("ascii")) != 16:
        raise CameraError("apk_layout", "Account cipher profile layout is not recognized.")
    target = private / "cloud-profile.json"
    value = {"source_apk_sha256": KNOWN_APK, "web_key": key}
    if target.exists():
        from .config import read_private_json

        if read_private_json(target) != value:
            raise CameraError("cloud_profile", "An existing account profile differs; stopped.")
    else:
        write_private_json(target, value)
    return {"cloud_profile_imported": True, "apk_sha256": KNOWN_APK}
