import asyncio

import pytest
from fastapi import WebSocketDisconnect

from arb.browser_bridge import BrowserBridge, ExtensionTransport, prepare_extension
from arb.var_errors import VarError
from fastapi.testclient import TestClient
from types import SimpleNamespace
from arb.server import create_app


class Socket:
    def __init__(self, origin="chrome-extension://" + "a" * 32):
        self.headers = {"origin": origin}
        self.incoming = asyncio.Queue()
        self.outgoing = asyncio.Queue()
        self.closed = None

    async def accept(self):
        pass

    async def close(self, code):
        self.closed = code

    async def receive_json(self):
        value = await self.incoming.get()
        if isinstance(value, Exception):
            raise value
        return value

    async def send_json(self, value):
        await self.outgoing.put(value)


async def connected_bridge(timeout=1):
    bridge, ws = BrowserBridge("test-token", timeout), Socket()
    await ws.incoming.put({"token": "test-token"})
    task = asyncio.create_task(bridge.serve(ws))
    assert await ws.outgoing.get() == {"type": "ready"}
    return bridge, ws, task


async def disconnect(ws, task):
    await ws.incoming.put(WebSocketDisconnect())
    await task


@pytest.mark.parametrize("origin,token", [("http://evil.test", "test-token"),
                                         ("chrome-extension://" + "a" * 32, "wrong")])
async def test_rejects_webpage_origin_and_wrong_capability(origin, token):
    bridge, ws = BrowserBridge("test-token"), Socket(origin)
    await ws.incoming.put({"token": token})
    await bridge.serve(ws)
    assert ws.closed == 1008 and not bridge.connected


async def test_authenticated_roundtrip_and_endpoint_restriction():
    bridge, ws, task = await connected_bridge()
    transport = ExtensionTransport(bridge)
    connecting = asyncio.create_task(transport.connect())
    handshake = await ws.outgoing.get()
    assert handshake["path"] == "/api/me" and handshake["new_session"] is True
    await ws.incoming.put({"id": handshake["id"], "result": [200, {"token": "session"}]})
    await connecting
    call = asyncio.create_task(transport.call("GET", "/api/me"))
    request = await ws.outgoing.get()
    await ws.incoming.put({"id": request["id"], "result": [200, {"token": "session"}]})
    assert await call == (200, {"token": "session"})
    with pytest.raises(VarError, match="hors périmètre"):
        await transport.call("POST", "/api/withdraw", {})
    assert ws.outgoing.empty()
    await disconnect(ws, task)


async def test_missing_omni_login_rejects_start_before_any_trading_requests():
    bridge, ws, task = await connected_bridge()
    connecting = asyncio.create_task(ExtensionTransport(bridge).connect())
    request = await ws.outgoing.get()
    assert request["method"] == "GET" and request["path"] == "/api/me"
    await ws.incoming.put({"id": request["id"], "result": [401, {}]})
    with pytest.raises(RuntimeError, match="Session Omni absente"):
        await connecting
    assert ws.outgoing.empty()
    await disconnect(ws, task)


async def test_extension_error_is_retained_for_diagnosis():
    bridge, ws, task = await connected_bridge()
    call = asyncio.create_task(bridge.call("GET", "/api/positions"))
    request = await ws.outgoing.get()
    await ws.incoming.put({"id": request["id"], "error": "Onglet Omni fermé"})
    with pytest.raises(VarError, match="fermé"):
        await call
    assert bridge.last_error == "Onglet Omni fermé"
    await disconnect(ws, task)


async def test_order_timeout_never_resends_and_late_response_is_ignored():
    bridge, ws, task = await connected_bridge(timeout=0.01)
    call = asyncio.create_task(bridge.call("POST", "/api/orders/new/market", {"quote_id": "q"}))
    request = await ws.outgoing.get()
    with pytest.raises(VarError, match="résultat inconnu"):
        await call
    assert not bridge.pending and ws.outgoing.empty()
    await ws.incoming.put({"id": request["id"], "result": [200, {"rfq_id": "late"}]})
    await disconnect(ws, task)


async def test_disconnect_fails_pending_order_without_resending():
    bridge, ws, task = await connected_bridge()
    call = asyncio.create_task(bridge.call("POST", "/api/orders/new/market", {}))
    await ws.outgoing.get()
    await disconnect(ws, task)
    with pytest.raises(VarError, match="déconnectée"):
        await call
    assert not bridge.connected and not bridge.pending and ws.outgoing.empty()


async def test_second_extension_cannot_replace_active_socket():
    bridge, ws, task = await connected_bridge()
    other = Socket()
    await other.incoming.put({"token": "test-token"})
    await bridge.serve(other)
    assert other.closed == 1008 and bridge.socket is ws
    await disconnect(ws, task)


def test_generated_local_capability_is_stable(tmp_path):
    (tmp_path / "browser_extension").mkdir()
    token = prepare_extension(tmp_path)
    assert len(token) >= 32 and prepare_extension(tmp_path) == token
    js = (tmp_path / "browser_extension" / "bridge_config.js").read_text(encoding="utf-8")
    assert token in js and "8792" in js


def test_server_exposes_bridge_only_with_extension_context():
    bridge = BrowserBridge("test-token")
    ctx = SimpleNamespace(browser_bridge=bridge)
    with TestClient(create_app(ctx), base_url="http://127.0.0.1") as client:
        assert client.get("/api/browser/status").json()["connected"] is False
        with client.websocket_connect("ws://127.0.0.1/api/browser/bridge", headers={"origin": "chrome-extension://" + "a" * 32}) as ws:
            ws.send_json({"token": "test-token"})
            assert ws.receive_json() == {"type": "ready"}
            assert client.get("/api/browser/status").json()["connected"] is True
        assert client.get("/api/browser/status").json()["connected"] is False
