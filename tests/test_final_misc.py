"""Vague finale : R2 (max_delta_usd suit fill_usd), S1 (var_liq_distance_min = 8 %), README."""
from pathlib import Path

from fastapi.testclient import TestClient

from arb.config import GlobalCfg, MarketCfg, load_config
from arb.server import create_app
from tests.test_server import Ctx

ROOT = Path(__file__).resolve().parents[1]


def _ctx_with(cfg):
    ctx = Ctx()
    ctx.orch.engines["BTC"].cfg = cfg
    return ctx


def test_r2_max_delta_follows_fill_usd_when_not_explicit():
    cfg = MarketCfg(var_asset="BTC", var_kind="x", hl_coin="c")
    assert cfg.max_delta_auto is True and cfg.max_delta_usd == 1500
    c = TestClient(create_app(_ctx_with(cfg)), base_url="http://127.0.0.1", headers={"x-auth-token": "tok"})
    assert c.post("/api/market/BTC/params", json={"fill_usd": 500}).status_code == 200
    assert cfg.fill_usd == 500 and cfg.max_delta_usd == 750


def test_r2_explicit_max_delta_kept():
    cfg = MarketCfg(var_asset="BTC", var_kind="x", hl_coin="c", max_delta_usd=2000)
    assert cfg.max_delta_auto is False
    c = TestClient(create_app(_ctx_with(cfg)), base_url="http://127.0.0.1", headers={"x-auth-token": "tok"})
    c.post("/api/market/BTC/params", json={"fill_usd": 500})
    assert cfg.max_delta_usd == 2000


def test_s1_var_liq_distance_min_is_8_percent():
    assert GlobalCfg().var_liq_distance_min == 0.08
    g, _ = load_config(ROOT / "config.toml")
    assert g.var_liq_distance_min == 0.08


def test_readme_known_limits_section():
    txt = (ROOT / "README.md").read_text()
    assert "## Limites connues" in txt
    sec = txt.split("## Limites connues", 1)[1]
    assert "ne se reconnecte pas" in sec and "qty" in sec and "is_reduce_only" in sec and "cleared" in sec
    assert "—" not in txt
