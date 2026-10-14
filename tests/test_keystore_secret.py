import os
import stat

import main


def test_reads_remembered_secret(tmp_path, monkeypatch):
    f = tmp_path / "keystore_secret"
    f.write_text("motdepasse\n")
    monkeypatch.setattr(main, "SECRET_FILE", f)
    assert main.read_keystore_secret() == ("motdepasse", True)


def test_prompts_when_no_file(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "SECRET_FILE", tmp_path / "absent")
    monkeypatch.setattr(main.getpass, "getpass", lambda prompt: "saisi")
    assert main.read_keystore_secret() == ("saisi", False)


def test_remember_writes_private_file(tmp_path, monkeypatch):
    f = tmp_path / "sub" / "keystore_secret"
    monkeypatch.setattr(main, "SECRET_FILE", f)
    monkeypatch.setattr("builtins.input", lambda prompt: "o")
    main.offer_to_remember("abc")
    assert f.read_text() == "abc"
    assert stat.S_IMODE(os.stat(f).st_mode) == 0o600


def test_remember_declined_writes_nothing(tmp_path, monkeypatch):
    f = tmp_path / "keystore_secret"
    monkeypatch.setattr(main, "SECRET_FILE", f)
    monkeypatch.setattr("builtins.input", lambda prompt: "")
    main.offer_to_remember("abc")
    assert not f.exists()
