import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tools.bootstrap_windows import prepare


@pytest.mark.parametrize("state", ["missing", "broken", "invalid_exe", "unprepared", "ready", "updated"])
def test_prepare_creates_repairs_and_installs_only_when_needed(tmp_path, state):
    root = tmp_path / "project with spaces"
    root.mkdir()
    lock = root / "requirements-lock.txt"
    lock.write_text("pytest==8.0.0\n")
    python = root / ".venv/Scripts/python.exe"
    stamp = root / ".venv/.requirements.sha256"
    if state != "missing":
        python.parent.mkdir(parents=True)
        python.touch()
    if state in ("ready", "updated"):
        stamp.write_text(hashlib.sha256(lock.read_bytes()).hexdigest() if state == "ready" else "old")
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        assert kwargs["cwd"] == root
        if "-c" in args:
            if state == "invalid_exe":
                raise OSError("Invalid executable")
            return subprocess.CompletedProcess(args, 1 if state == "broken" else 0)
        if "venv" in args:
            python.parent.mkdir(parents=True, exist_ok=True)
            python.touch()
        return subprocess.CompletedProcess(args, 0)

    assert prepare(root, run, "base-python") == 0
    assert any("venv" in call for call in calls) == (state in ("missing", "broken", "invalid_exe"))
    assert any("install" in call for call in calls) == (state != "ready")
    assert stamp.read_text().strip() == hashlib.sha256(lock.read_bytes()).hexdigest()


def test_install_failure_is_not_marked_ready_and_can_be_retried(tmp_path):
    (tmp_path / "requirements-lock.txt").write_text("pytest==8.0.0\n")
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        if "venv" in args:
            (tmp_path / ".venv/Scripts").mkdir(parents=True)
            (tmp_path / ".venv/Scripts/python.exe").touch()
        if "install" in args:
            raise subprocess.CalledProcessError(1, args)
        return subprocess.CompletedProcess(args, 0)

    assert prepare(tmp_path, run, "base-python") == 1
    assert not (tmp_path / ".venv/.requirements.sha256").exists()
    assert prepare(tmp_path, lambda *a, **k: subprocess.CompletedProcess(a, 0)) == 0
    assert (tmp_path / ".venv/.requirements.sha256").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows cmd launcher")
@pytest.mark.parametrize("exit_code", [0, 7])
def test_cmd_finds_python_without_venv_and_propagates_failure(tmp_path, exit_code):
    if shutil.which("py") is None:
        pytest.skip("Python launcher not installed")
    probe = subprocess.run(["py", "-3.12", "-V"], capture_output=True)
    if probe.returncode:
        pytest.skip("Python 3.12 not installed")
    root = tmp_path / "project with spaces"
    (root / "tools").mkdir(parents=True)
    source = Path(__file__).resolve().parents[1]
    shutil.copyfile(source / "tools/ensure_windows_env.cmd", root / "tools/ensure_windows_env.cmd")
    (root / "tools/bootstrap_windows.py").write_text(
        f"import sys\nprint('BOOTSTRAP_EXECUTED')\nsys.exit({exit_code})\n")
    result = subprocess.run(["cmd.exe", "/d", "/c", "tools\\ensure_windows_env.cmd"],
                            cwd=root, capture_output=True, text=True)
    assert result.returncode == exit_code, result.stdout + result.stderr
    assert "BOOTSTRAP_EXECUTED" in result.stdout
