"""Prepare the local Python environment without starting the bot."""
import hashlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def prepare(root=ROOT, run=subprocess.run, executable=sys.executable):
    root = Path(root)
    python = root / ".venv" / "Scripts" / "python.exe"
    lock = root / "requirements-lock.txt"
    stamp = python.parent.parent / ".requirements.sha256"
    try:
        fingerprint = hashlib.sha256(lock.read_bytes()).hexdigest()
        try:
            usable = python.is_file() and run(
                [str(python), "-c", "import sys; sys.exit(sys.version_info[:2] != (3, 12))"],
                cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            ).returncode == 0
        except OSError:
            usable = False
        if not usable:
            print("Creation/reparation de .venv avec Python 3.12...", flush=True)
            run([executable, "-m", "venv", str(python.parent.parent)], cwd=root, check=True)
        if not usable or not stamp.is_file() or stamp.read_text().strip() != fingerprint:
            print("Installation des dependances (premier lancement ou mise a jour)...", flush=True)
            run([str(python), "-m", "pip", "install", "-r", str(lock)], cwd=root, check=True)
            run([str(python), "-m", "pip", "check"], cwd=root, check=True)
            stamp.write_text(fingerprint + "\n")
        return 0
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"Preparation impossible : {exc}")
        print("Verifiez Python 3.12 et la connexion Internet, puis relancez le fichier .cmd.")
        return 1


if __name__ == "__main__":
    raise SystemExit(prepare())
