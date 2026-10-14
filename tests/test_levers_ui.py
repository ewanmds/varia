"""L5 (docs/superpowers/levers-2026-09-23.md) : mode d'entrée modifiable par marché, à chaud, via
un endpoint authentifié ; affichage des âges et du basis rapide."""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from arb.config import MarketCfg
from arb.server import create_app
from tests.test_server import Journal, OkAuth


class Eng:
    def __init__(self, cfg):
        self.cfg = cfg
        self.modes = []

    async def set_entry_mode(self, mode):
        if mode not in ("chase", "maker", "lag_taker"):
            raise ValueError(mode)
        self.modes.append(mode)
        self.cfg.entry_mode = mode


class Orch:
    def __init__(self, cfgs):
        self.engines = {c.var_asset: Eng(c) for c in cfgs}

    def status(self):
        return {"markets": [], "killed": False}


def _cfgs():
    return [MarketCfg(var_asset="USOILP", var_kind="commodity", hl_coin="xyz:CL"),
            MarketCfg(var_asset="XAUS", var_kind="commodity", hl_coin="xyz:GOLD")]


class Ctx:
    def __init__(self, running=True):
        self.markets = _cfgs()
        self.orch = Orch(self.markets) if running else None
        self.journal = Journal()
        self.auth = OkAuth()
        self.paper = True


def _client(ctx, token="tok"):
    headers = {"x-auth-token": token} if token else {}
    return TestClient(create_app(ctx), base_url="http://127.0.0.1", headers=headers)


@pytest.mark.parametrize("mode", ["chase", "lag_taker", "maker"])
def test_entry_mode_endpoint_switches_running_engine(mode):
    ctx = Ctx()
    res = _client(ctx).post("/api/market/USOILP/entry_mode", json={"mode": mode})
    assert res.status_code == 200 and res.json() == {"ok": True, "entry_mode": mode}
    assert ctx.orch.engines["USOILP"].cfg.entry_mode == mode
    assert ctx.orch.engines["XAUS"].cfg.entry_mode == "chase"


def test_entry_mode_endpoint_goes_through_engine_to_cancel_orders():
    ctx = Ctx()
    _client(ctx).post("/api/market/USOILP/entry_mode", json={"mode": "lag_taker"})
    assert ctx.orch.engines["USOILP"].modes == ["lag_taker"]


@pytest.mark.parametrize("body", [{"mode": "yolo"}, {"mode": None}, {}, {"mode": 3}])
def test_entry_mode_endpoint_rejects_invalid(body):
    ctx = Ctx()
    assert _client(ctx).post("/api/market/USOILP/entry_mode", json=body).status_code == 400
    assert ctx.orch.engines["USOILP"].cfg.entry_mode == "chase"


def test_entry_mode_endpoint_unknown_market():
    assert _client(Ctx()).post("/api/market/NOPE/entry_mode", json={"mode": "maker"}).status_code == 404


def test_entry_mode_endpoint_requires_auth():
    ctx = Ctx()
    for token in (None, "bad"):
        assert _client(ctx, token).post("/api/market/USOILP/entry_mode",
                                        json={"mode": "lag_taker"}).status_code == 400
    assert ctx.orch.engines["USOILP"].cfg.entry_mode == "chase"


def test_entry_mode_endpoint_before_start_updates_config():
    ctx = Ctx(running=False)
    assert _client(ctx).post("/api/market/USOILP/entry_mode", json={"mode": "maker"}).status_code == 200
    assert ctx.markets[0].entry_mode == "maker"


def test_ui_shows_new_columns_and_mode_selector():
    web = Path(__file__).resolve().parents[1] / "web"
    html = (web / "index.html").read_text()
    js = (web / "app.js").read_text()
    for label in ("Âge Var ws", "Âge bbo HL", "Basis rapide"):
        assert label in html
    assert "/entry_mode" in js and "lag_taker" in js
    for key in ("var_ws_age", "hl_bbo_age", "basis_fast"):
        assert key in js
    assert "\u2014" not in html and "\u2014" not in js     # pas de tiret cadratin
