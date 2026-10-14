import math
import pytest
from arb.config import MarketCfg
from arb.pricing import (VarQuote, round_px, round_sz, inventory_ratio, side_margins_bps,
                         maker_prices, BasisEma, sanity_ok, edge_bps)

def cfg(**kw):
    base = dict(var_asset="XAGS", var_kind="commodity", hl_coin="xyz:SILVER")
    base.update(kw)
    return MarketCfg(**base)

def test_round_px_5_sig_figs_and_direction():
    assert round_px(67.12345, 2, True) == 67.123
    assert round_px(67.12345, 2, False) == 67.124
    assert round_px(30712.37, 3, True) == 30712.0
    assert round_px(30712.37, 3, False) == 30713.0
    # decimales max = 6 - szDecimals
    assert round_px(1.2345678, 5, True) == 1.2

def test_round_sz_floors():
    assert round_sz(14.9999, 2) == 14.99
    assert round_sz(0.0329, 3) == 0.032

def test_inventory_ratio_clamped():
    assert inventory_ratio(1500, 3000) == 0.5
    assert inventory_ratio(-9000, 3000) == -1.0

def test_side_margins_flat():
    b, s = side_margins_bps(cfg(), fee_bps=0.2, ratio=0.0, wide_mult=1.0)
    assert b == pytest.approx(7.2) and s == pytest.approx(7.2)

def test_side_margins_long_inventory_tightens_sell():
    b, s = side_margins_bps(cfg(), fee_bps=0.2, ratio=0.5, wide_mult=1.0)
    exit_total = 0 + 0.2 + 2
    assert s == pytest.approx(7.2 - 0.5 * (7.2 - exit_total))
    assert b == pytest.approx(7.2 + 0.5 * (7.2 - exit_total))

def test_side_margins_full_inventory_removes_loading_side():
    b, s = side_margins_bps(cfg(), 0.2, 1.0, 1.0)
    assert b is None and s == pytest.approx(0 + 0.2 + 2)
    b, s = side_margins_bps(cfg(), 0.2, -1.0, 1.0)
    assert s is None and b is not None

def test_wide_mult_scales_base_margin():
    b, s = side_margins_bps(cfg(), 0.0, 0.0, 2.0)
    assert b == pytest.approx(14.0)

def test_maker_prices_basic():
    q = VarQuote(bid=67.00, ask=67.10, qty=15, ts=0)
    buy, sell = maker_prices(q, 0.0, cfg(), 0.0, 0.0, 1.0, hl_bid=66.9, hl_ask=67.2, sz_decimals=2)
    assert buy == round_px(67.00 * (1 - 7e-4), 2, True)
    assert sell == round_px(67.10 * (1 + 7e-4), 2, False)

def test_maker_prices_stays_post_only():
    q = VarQuote(bid=67.00, ask=67.10, qty=15, ts=0)
    # le carnet HL est très bas : notre achat croiserait l'ask HL -> on se place sous l'ask
    buy, sell = maker_prices(q, 0.0, cfg(), 0.0, 0.0, 1.0, hl_bid=66.0, hl_ask=66.5, sz_decimals=2)
    assert buy < 66.5
    # le carnet HL est très haut : notre vente croiserait le bid HL -> on se place au-dessus
    buy, sell = maker_prices(q, 0.0, cfg(), 0.0, 0.0, 1.0, hl_bid=68.0, hl_ask=68.2, sz_decimals=2)
    assert sell > 68.0

def test_maker_prices_basis_shift():
    q = VarQuote(bid=89.60, ask=89.70, qty=10, ts=0)
    buy0, _ = maker_prices(q, 0.0, cfg(), 0.0, 0.0, 1.0, None, None, 2)
    buy1, _ = maker_prices(q, 0.30, cfg(), 0.0, 0.0, 1.0, None, None, 2)
    assert buy1 > buy0

def test_basis_ema_converges():
    e = BasisEma(tau_s=60)
    assert e.value is None
    e.update(100.3, 100.0, ts=0)
    assert e.value == pytest.approx(0.3)
    for t in range(1, 1000):
        e.update(100.5, 100.0, ts=t)
    assert e.value == pytest.approx(0.5, abs=1e-3)
    assert e.deviation_bps(100.5, 100.0) == pytest.approx(0.0, abs=0.2)

def test_sanity_and_edge():
    assert sanity_ok(100.0, 100.2, 30)
    assert not sanity_ok(100.0, 100.4, 30)
    # achat HL à 99.93, vente Var à 100.00, frais 0.2 pb
    assert edge_bps(True, 99.93, 100.00, 0.2) == pytest.approx(7.0049 - 0.2, abs=1e-3)
    # vente HL à 100.07, rachat Var à 100.00
    assert edge_bps(False, 100.07, 100.00, 0.2) == pytest.approx(7.0 - 0.2, abs=1e-3)
