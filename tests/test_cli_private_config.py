import json
import sys

from v360_web.cli import main
from v360_web.config import write_private_json


def test_device_configuration_preserves_pairing_and_does_not_print_credentials(
    tmp_path, monkeypatch, capsys
):
    path = tmp_path / "device.json"
    token = "fake-existing-pairing-token-1234567890"
    password = "fake-device-password"
    write_private_json(path, {"device": {}, "bridge": {"token": token}})
    answers = iter(["MATE-test", "0", "n"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    prompts = []

    def secret(prompt):
        prompts.append(prompt)
        return password

    monkeypatch.setattr("getpass.getpass", secret)
    monkeypatch.setattr(sys, "argv", ["v360", "--config", str(path), "configure"])
    assert main() == 0
    stored = json.loads(path.read_text())
    assert stored["bridge"]["token"] == token
    assert stored["device"]["password"] == password
    assert len(prompts) == 1
    output = capsys.readouterr().out
    assert token not in output and password not in output


def test_native_configuration_does_not_prompt_for_phone_token_or_select_windows(
    tmp_path, monkeypatch
):
    path = tmp_path / "device.json"
    write_private_json(path, {"device": {}, "bridge": {"adapter": "native-arm64"}})
    answers = iter(["MATE-test", "12345", "n"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    prompts = []

    def secret(prompt):
        prompts.append(prompt)
        return "fake-device-password"

    monkeypatch.setattr("getpass.getpass", secret)
    monkeypatch.setattr("v360_web.cli.platform.system", lambda: "Linux")
    monkeypatch.setattr("v360_web.cli.shutil.which", lambda _: "/fake/windows/python.exe")
    monkeypatch.setattr(sys, "argv", ["v360", "--config", str(path), "configure"])
    assert main() == 0
    assert json.loads(path.read_text())["bridge"] == {"adapter": "native-arm64"}
    assert len(prompts) == 1
