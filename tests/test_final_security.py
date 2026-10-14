"""Vague finale : sécurité du serveur (I7), déblocage (I5), keystore (R3)."""
import sys

import pytest
from fastapi.testclient import TestClient

from arb.engine import BLOCKED, IDLE, QUOTING
from arb.keystore import KeyStore, KeyStoreError
from arb.server import create_app
from tests.test_server import Ctx, Engine, OkAuth


def client(ctx, token="tok", host="127.0.0.1"):
    headers = {"x-auth-token": token} if token else {}
    return TestClient(create_app(ctx), base_url=f"http://{host}", headers=headers)


class StartCtx(Ctx):
    def __init__(self):
        super().__init__()
        self.started = []
        self.stopped = 0

    async def start_bot(self, account):
        self.started.append(account)

    async def stop_bot(self):
        self.stopped += 1


# -- I7 -----------------------------------------------------------------------------------------

def test_i7_foreign_host_rejected():
    ctx = Ctx()
    assert client(ctx, host="evil.example").get("/api/status").status_code == 400
    assert client(ctx, host="localhost").get("/api/status").status_code == 200
    assert client(ctx).get("/api/status").status_code == 200


@pytest.mark.parametrize("path,body", [
    ("/api/bot/stop", None),
    ("/api/bot/kill", None),
    ("/api/bot/start", {"account_address": "0x" + "ab" * 20}),
    ("/api/market/BTC/toggle", None),
    ("/api/market/BTC/params", {"fill_usd": 500}),
    ("/api/market/BTC/unblock", None),
    ("/api/params/min_edge", {"bps": 8}),
    ("/api/agent/approve_challenge", {"account_address": "0x" + "ab" * 20}),
    ("/api/agent/approve_submit", {"account_address": "0x" + "ab" * 20, "nonce": 1, "signature": "0x"}),
])
def test_i7_mutating_endpoints_require_session(path, body):
    ctx = StartCtx()
    eng = ctx.orch.engines["BTC"]
    eng.state = BLOCKED
    c = client(ctx, token=None)
    res = c.post(path, json=body) if body is not None else c.post(path)
    assert res.status_code == 400
    # aucun effet de bord
    assert ctx.orch.killed is False and ctx.stopped == 0 and ctx.started == []
    assert eng.cancelled == 0 and eng.resumed == 0 and eng.cfg.fill_usd == 1000.0 and eng.cfg.enabled
    assert eng.cfg.min_edge_bps == 5.0


def test_i7_no_auth_object_refuses_mutations():
    ctx = Ctx()
    ctx.auth = None
    assert client(ctx).post("/api/bot/kill").status_code == 400
    assert ctx.orch.killed is False


def test_i7_valid_session_allows_kill_and_stop():
    ctx = StartCtx()
    c = client(ctx)
    assert c.post("/api/bot/kill").status_code == 200 and ctx.orch.killed
    assert c.post("/api/bot/stop").status_code == 200 and ctx.stopped == 1


def test_i7_ui_sends_session_header():
    from pathlib import Path
    js = (Path(__file__).resolve().parents[1] / "web" / "app.js").read_text(encoding="utf-8")
    assert 'headers["x-auth-token"] = state.token' in js


# -- I5 : déblocage -----------------------------------------------------------------------------

def test_i5_unblock_resumes_blocked_market():
    ctx = Ctx()
    eng = ctx.orch.engines["BTC"]
    eng.state = BLOCKED
    res = client(ctx).post("/api/market/BTC/unblock")
    assert res.status_code == 200 and res.json()["state"] == QUOTING
    assert eng.resumed == 1


def test_i5_unblock_disabled_market_stays_idle():
    ctx = Ctx()
    eng = ctx.orch.engines["BTC"]
    eng.state = BLOCKED
    eng.cfg.enabled = False
    assert client(ctx).post("/api/market/BTC/unblock").json()["state"] == IDLE


def test_i5_unblock_refuses_non_blocked_and_unknown():
    ctx = Ctx()
    assert client(ctx).post("/api/market/BTC/unblock").status_code == 400
    assert client(ctx).post("/api/market/DOGE/unblock").status_code == 404


def test_i5_ui_has_unblock_button():
    from pathlib import Path
    js = (Path(__file__).resolve().parents[1] / "web" / "app.js").read_text(encoding="utf-8")
    assert "/unblock" in js and "Débloquer" in js


# -- R3 -----------------------------------------------------------------------------------------

def test_r3_wrong_password_raises_missing_file_is_empty(tmp_path):
    from eth_account import Account
    key = "0x" + "22" * 32
    addr = Account.from_key(key).address.lower()
    assert KeyStore(tmp_path / "s.enc", "bon").save_agent("0x" + "cd" * 20, key, addr)
    with pytest.raises(KeyStoreError):
        KeyStore(tmp_path / "s.enc", "faux").load_agents()
    assert KeyStore(tmp_path / "absent.enc", "nimporte").load_agents() == {}


def test_r3_main_refuses_to_start_on_wrong_password(tmp_path, monkeypatch):
    import main as mainmod
    from eth_account import Account
    key = "0x" + "33" * 32
    KeyStore(tmp_path / "secrets.enc", "bon").save_agent("0x" + "cd" * 20, key,
                                                         Account.from_key(key).address.lower())
    monkeypatch.setattr(mainmod, "ROOT", tmp_path)
    monkeypatch.setattr(mainmod, "read_keystore_secret", lambda: ("faux", True))
    config = tmp_path / "config.toml"
    config.write_text('[global]\n[pair]\nenabled = false\n', encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["main.py", "--config", str(config)])

    def no_run(*a, **k):
        raise AssertionError("le serveur ne doit pas démarrer")

    monkeypatch.setattr(mainmod.uvicorn, "run", no_run)
    with pytest.raises(SystemExit) as ei:
        mainmod.main()
    assert "Mot de passe incorrect" in str(ei.value)


def test_ui_emergency_stop_stays_available_during_normal_stop():
    from pathlib import Path
    js = (Path(__file__).resolve().parents[1] / "web" / "app.js").read_text()
    assert '$("btn_kill").disabled = killing;' in js
    assert 'if (state.stopping === "kill") return;' in js
