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
