from arb.config import GlobalCfg, MarketCfg
from arb.risk import quote_block_reason, ReconcileTracker

G = GlobalCfg()
M = MarketCfg(var_asset="XAGS", var_kind="commodity", hl_coin="xyz:SILVER")

def ok_kwargs(**kw):
    base = dict(var_age=1.0, hl_stalled=False, fills_alive=True, strikes_suspended=False, sanity=True,
                basis_dev_bps=0.0, cfg=M, g=G, roll_window=False, daily_pnl=0.0, gross_hl_usd=0.0,
                gross_var_usd=0.0, hl_margin_ratio=0.1, var_liq_distance=None, session_ok=True,
                book_stale=False)
    base.update(kw)
    return base

def test_all_ok():
    assert quote_block_reason(**ok_kwargs()) is None

def test_each_rule_blocks():
    cases = [dict(var_age=5.5), dict(hl_stalled=True), dict(fills_alive=False), dict(book_stale=True),
             dict(strikes_suspended=True),
             dict(sanity=False), dict(daily_pnl=-51), dict(gross_hl_usd=10001), dict(gross_var_usd=10001),
             dict(hl_margin_ratio=0.7), dict(var_liq_distance=0.03), dict(session_ok=False)]
    for c in cases:
        assert quote_block_reason(**ok_kwargs(**c)) is not None, c


def test_book_stale_gives_dedicated_reason():
    assert quote_block_reason(**ok_kwargs(book_stale=True)) == "carnet HL périmé"

def test_roll_and_basis_only_for_ema_markets():
    oil = MarketCfg(var_asset="USOILP", var_kind="commodity", hl_coin="xyz:CL", basis_mode="ema", roll_block=True)
    assert quote_block_reason(**ok_kwargs(cfg=oil, roll_window=True)) is not None
    assert quote_block_reason(**ok_kwargs(cfg=oil, basis_dev_bps=31)) is not None
    assert quote_block_reason(**ok_kwargs(roll_window=True)) is None

def test_reconcile_needs_two_reads():
    t = [0.0]; r = ReconcileTracker(clock=lambda: t[0])
    assert r.observe("XAGS", 5, True, False, 1000, 1500) == "ok"
    assert r.observe("XAGS", 800, True, False, 1000, 1500) == "wait"
    assert r.observe("XAGS", 790, True, False, 1000, 1500) == "correct"
    r.mark_corrected("XAGS")
    assert r.observe("XAGS", 800, True, False, 1000, 1500) == "wait"
    assert r.observe("XAGS", 800, True, False, 1000, 1500) == "block"   # déjà corrigé dans l'heure
    t[0] = 3700
    assert r.observe("XAGS", 800, True, False, 1000, 1500) == "wait"
    assert r.observe("XAGS", 800, True, False, 1000, 1500) == "correct"

def test_reconcile_stale_or_inflight_waits_and_big_blocks():
    r = ReconcileTracker(clock=lambda: 0.0)
    assert r.observe("XAGS", 800, False, False, 1000, 1500) == "wait"
    assert r.observe("XAGS", 800, True, True, 1000, 1500) == "wait"
    assert r.observe("XAGS", 1600, True, False, 1000, 1500) == "block"
