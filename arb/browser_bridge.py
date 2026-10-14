"""Transport local pour la session Omni du Chrome habituel, via extension."""
from __future__ import annotations

import asyncio
import json
import secrets
from pathlib import Path

from fastapi import WebSocket, WebSocketDisconnect

from arb.var_errors import VarError

ALLOWED = {
    ("GET", "/api/me"), ("GET", "/api/positions"),
    ("GET", "/api/portfolio"), ("GET", "/api/orders/v2"),
    ("GET", "/api/metadata/supported_assets"),
    ("POST", "/api/quotes/indicative"), ("POST", "/api/orders/new/market"),
    ("POST", "/api/quotes/accept"), ("POST", "/api/settlement_pools/set_leverage"),
}


def prepare_extension(root: Path) -> str:
    path = root / "data" / "browser-bridge-token"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        try:
            with path.open("x", encoding="utf-8") as f:
                f.write(secrets.token_urlsafe(32))
        except FileExistsError:
            pass
    token = path.read_text(encoding="utf-8").strip()
    if len(token) < 32:
        raise RuntimeError("Jeton du pont Chrome invalide")
    config = {"token": token, "ports": [8792, 8791]}
    (root / "browser_extension" / "bridge_config.js").write_text(
        "globalThis.BOT_BRIDGE = " + json.dumps(config) + ";\n", encoding="utf-8")
    return token


class BrowserBridge:
    def __init__(self, token: str, timeout_s: float = 30):
        self.token, self.timeout_s = token, timeout_s
        self.socket = None
        self.pending = {}
        self.last_error = None
        self._send_lock = asyncio.Lock()

    @property
    def connected(self):
        return self.socket is not None

    async def serve(self, ws: WebSocket):
        origin = ws.headers.get("origin", "")
        if not origin.startswith("chrome-extension://"):
            await ws.close(code=1008)
            return
        await ws.accept()
        try:
            hello = await asyncio.wait_for(ws.receive_json(), 5)
            if not isinstance(hello, dict) or not secrets.compare_digest(str(hello.get("token", "")), self.token):
                await ws.close(code=1008)
                return
            if self.socket is not None:
                await ws.close(code=1008)
                return
            self.socket = ws
            await ws.send_json({"type": "ready"})
            while True:
                msg = await asyncio.wait_for(ws.receive_json(), 45)
                if not isinstance(msg, dict) or msg.get("type") == "ping":
                    continue
                future = self.pending.get(msg.get("id"))
                if future is not None and not future.done():
                    if msg.get("error"):
                        self.last_error = str(msg["error"])
                        future.set_exception(VarError(self.last_error))
                    else:
                        result = msg.get("result")
                        if not isinstance(result, list) or len(result) != 2 or not isinstance(result[0], int):
                            future.set_exception(VarError("Réponse du pont Chrome invalide"))
                        else:
                            self.last_error = None
                            future.set_result((result[0], result[1]))
        except (WebSocketDisconnect, asyncio.TimeoutError, ValueError, RuntimeError):
            pass
        finally:
            if self.socket is ws:
                self.socket = None
                for future in tuple(self.pending.values()):
                    if not future.done():
                        future.set_exception(VarError("Extension Chrome déconnectée"))

    async def call(self, method, path, body=None, *, new_session=False):
        if (method, path) not in ALLOWED:
            raise VarError("Requête hors périmètre du pont Chrome")
        if new_session and (method, path) != ("GET", "/api/me"):
            raise VarError("La sélection d'onglet exige une lecture de session")
        if not self.connected:
            raise VarError("Extension non connectée : charger browser_extension dans le Chrome habituel")
        request_id = secrets.token_hex(16)
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            async with self._send_lock:
                if self.socket is None:
                    raise VarError("Extension Chrome déconnectée")
                await self.socket.send_json({"id": request_id, "method": method, "path": path, "body": body,
                                             "new_session": new_session})
            return await asyncio.wait_for(future, self.timeout_s)
        except asyncio.TimeoutError as exc:
            # Aucun renvoi : un ordre ayant expiré peut avoir été exécuté.
            raise VarError("Délai du pont Chrome dépassé, résultat inconnu") from exc
        finally:
            self.pending.pop(request_id, None)
            if not future.done():
                future.cancel()


class ExtensionTransport:
    def __init__(self, bridge: BrowserBridge):
        self.bridge = bridge

    async def connect(self):
        if not self.bridge.connected:
            raise RuntimeError("Extension non connectée : charger browser_extension dans chrome://extensions")
        status, data = await self.bridge.call("GET", "/api/me", new_session=True)
        if status != 200 or not isinstance(data, dict) or not data.get("token"):
            raise RuntimeError("Session Omni absente : reconnecter le wallet dans l'onglet Omni")

    async def call(self, method, path, body=None):
        return await self.bridge.call(method, path, body)

    async def close(self):
        pass  # Le pont reste disponible pour le prochain démarrage du bot.
