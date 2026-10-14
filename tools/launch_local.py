"""Lance l'interface réelle ou réutilise son serveur existant, sans lancer les ordres."""
import json
import socket
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def is_bot_status(data):
    return isinstance(data, dict) and data.get("mode") in ("pair", "arb") and data.get("paper") is False and isinstance(data.get("running"), bool)


def main():
    with socket.socket() as probe:
        probe.settimeout(1)
        occupied = probe.connect_ex(("127.0.0.1", 8792)) == 0
    if occupied:
        try:
            with urllib.request.urlopen("http://127.0.0.1:8792/api/status", timeout=5) as response:
                if is_bot_status(json.load(response)):
                    print("Interface deja lancee : http://127.0.0.1:8792")
                    print("Utilisez cette interface ; aucun second serveur n'a ete lance.")
                    return 0
        except Exception:
            pass
        print("Port 8792 deja utilise par un serveur non reconnu. Aucun second serveur lance.")
        return 1
    return subprocess.call([sys.executable, str(ROOT / "main.py"), "--port", "8792"], cwd=ROOT)


if __name__ == "__main__":
    raise SystemExit(main())
