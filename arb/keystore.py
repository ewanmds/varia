"""Encrypted at-rest store for the trading agent key.

Minimal replica of Mega_MM's EncryptedKeyStore, isolated to this project. The
agent private key is encrypted with Fernet, the key being derived from a
`secret` string. If `secret` is empty the store stays disabled and keys are
kept in memory only (a warning is printed).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, Optional


def normalize_address(address: str) -> str:
    addr = str(address or "").strip().lower()
    if addr and not re.fullmatch(r"0x[a-f0-9]{40}", addr):
        return ""
    return addr


class KeyStoreError(Exception):
    """R3 : `secrets.enc` existe mais ne se déchiffre pas (mot de passe incorrect ou fichier
    corrompu). Le bot refuse alors de démarrer au lieu de repartir d'un keystore vide."""


def _derive_fernet_key(secret: str) -> bytes:
    key_bytes = hashlib.pbkdf2_hmac(
        "sha256",
        secret.encode("utf-8"),
        salt=b"bot_arb_varia_fernet_key",
        iterations=600_000,
        dklen=32,
    )
    return base64.urlsafe_b64encode(key_bytes)


class KeyStore:
    def __init__(self, path: Path, secret: str, *, warn_if_disabled: bool = True) -> None:
        self.path = path
        self._fernet = None
        self.enabled = False
        self.error: Optional[str] = None
        raw_secret = str(secret or "").strip()
        if not raw_secret:
            if warn_if_disabled:
                print(
                    "[WARNING] No keystore secret provided. The agent key will NOT be "
                    "encrypted at rest (kept in memory only).",
                    flush=True,
                )
            return
        try:
            from cryptography.fernet import Fernet
        except Exception:
            self.error = "cryptography package not available"
            return
        try:
            self._fernet = Fernet(_derive_fernet_key(raw_secret))
            self.enabled = True
        except Exception as exc:
            self.error = f"failed to init key store: {exc}"

    def _read(self) -> Dict[str, Any]:
        if not self.enabled or self._fernet is None or not self.path.exists():
            return {}
        try:
            obj = json.loads(self._fernet.decrypt(self.path.read_bytes()).decode("utf-8"))
        except Exception as exc:
            raise KeyStoreError(f"mot de passe incorrect ou keystore illisible ({self.path})") from exc
        return obj if isinstance(obj, dict) else {}

    def _write(self, payload: Dict[str, Any]) -> bool:
        if not self.enabled or self._fernet is None:
            return False
        try:
            blob = self._fernet.encrypt(json.dumps(payload).encode("utf-8"))
            tmp = self.path.with_suffix(".tmp")
            tmp.write_bytes(blob)
            try:
                os.chmod(tmp, 0o600)
            except Exception:
                pass
            tmp.replace(self.path)
            return True
        except Exception:
            return False

    def load_agents(self) -> Dict[str, Dict[str, str]]:
        """Return {account -> {key, agent_address, agent_name}}."""
        payload = self._read()
        out: Dict[str, Dict[str, str]] = {}
        agents = payload.get("agents", {})
        if not isinstance(agents, dict):
            return out
        for account, rec in agents.items():
            if not isinstance(rec, dict):
                continue
            norm = normalize_address(account)
            key = str(rec.get("key", "")).strip()
            addr = normalize_address(str(rec.get("agent_address", "")))
            if not norm or not key.startswith("0x") or not addr.startswith("0x"):
                continue
            out[norm] = {
                "key": key,
                "agent_address": addr,
                "agent_name": str(rec.get("agent_name", "")).strip() or "BOTARB",
            }
        return out

    def save_agent(self, account: str, key: str, agent_address: str, agent_name: str = "BOTARB") -> bool:
        norm = normalize_address(account)
        value = str(key or "").strip()
        addr = normalize_address(agent_address)
        if not norm or not value.startswith("0x") or not addr.startswith("0x"):
            return False
        if not self.enabled:
            return False
        payload = self._read()
        agents = payload.get("agents", {})
        if not isinstance(agents, dict):
            agents = {}
        agents[norm] = {
            "key": value,
            "agent_address": addr,
            "agent_name": str(agent_name or "BOTARB").strip() or "BOTARB",
        }
        payload["v"] = 1
        payload["updated_at"] = int(time.time())
        payload["agents"] = agents
        return self._write(payload)

    def has_persisted(self) -> bool:
        return self.enabled and self.path.exists()
