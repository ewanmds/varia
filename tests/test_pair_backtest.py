"""Tests du backtest de paire WTI/Brent (tools/pair_backtest.py) sur de petites séries synthétiques."""
import numpy as np
import pandas as pd
import pytest

from tools import pair_backtest as pb

T0 = int(pd.Timestamp("2026-09-08 00:00", tz="UTC").value // 1_000_000)
MIN = pb.MIN_MS


def _cost(h_w=0.0, h_b=0.0):
    return pb.CostModel({pb.WTI: {-1: h_w}, pb.BRENT: {-1: h_b}})


def _market(logr, start=T0, brent=100.0, sessions=None):
    """Marché synthétique : Brent fixe, WTI = Brent * exp(logr), open = close (pas de mouvement intra-minute)."""
    logr = np.asarray(logr, float)
    n = len(logr)
    ts = start + np.arange(n, dtype=np.int64) * MIN
    if sessions is not None:      # liste de longueurs ; 61 min de trou entre deux séances
        ts, cur = [], start
        for L in sessions:
            ts += [cur + k * MIN for k in range(L)]
            cur = ts[-1] + 61 * MIN
        ts = np.array(ts, dtype=np.int64)
    cb = np.full(n, brent)
    cw = cb * np.exp(logr / 1e4)
    df = pd.DataFrame({"ts": ts, "ow": cw, "cw": cw, "ob": cb, "cb": cb})
    return pb.Market.from_df(df)


def _params(**kw):
    base = dict(method="bps", window=10, entry=10.0, exit=0.0, slip_bps=0.0, delay_s=0.0)
    base.update(kw)
    return pb.Params(**base)


def test_fill_price_arithmetic():
    assert pb.fill_price(100.0, +1, 0.5, 0.5) == pytest.approx(100.01)
    assert pb.fill_price(100.0, -1, 0.335, 0.5) == pytest.approx(100.0 * (1 - 0.835e-4))
    assert pb.intrabar_mid(100.0, 106.0, 30) == pytest.approx(103.0)
    assert pb.intrabar_mid(100.0, 106.0, 0) == pytest.approx(100.0)


def test_execute_pair_costs_and_tranches():
    m = _market(np.zeros(5))
    p = _params(slip_bps=1.0)
    c = _cost(0.5, 0.25)
    ent = pb.execute_pair(m, 1, +1, p, c, closing=False)
    # 4 tranches de 2 500 $ : 10 000 $ de volume (au prix payé, à peu près)
    assert ent["vol"] == pytest.approx(10_000, rel=1e-3)
    # coût : 5 000 $ de WTI à 1,5 pb et 5 000 $ de Brent à 1,25 pb
    assert ent["fees"] == pytest.approx(5000 * 1.5e-4 + 5000 * 1.25e-4, rel=1e-3)
    ext = pb.execute_pair(m, 2, +1, p, c, closing=True, qty=ent["q"])
    # prix inchangés : la perte est exactement la somme des coûts payés
    assert ent["cash"] + ext["cash"] == pytest.approx(-(ent["fees"] + ext["fees"]), rel=1e-9)


def test_leg_imbalance_valued_at_send_time():
    """WTI monte pendant la minute d'exécution : les tranches WTI achetées plus tard coûtent plus cher."""
    df = pd.DataFrame({"ts": T0 + np.arange(3) * MIN, "ow": [100.0, 100.0, 100.0], "cw": [100.0, 106.0, 106.0],
                       "ob": [100.0] * 3, "cb": [100.0] * 3})
    m = pb.Market.from_df(df)
    p = _params(delay_s=0.0, spacing_s=1.0)
    ent = pb.execute_pair(m, 1, +1, p, _cost(), closing=False)
    # tranches WTI aux secondes 0 et 2 : mids 100 et 100,2
    assert ent["q"][0] == pytest.approx(25 + 2500 / 100.2)
    assert ent["drift"] == pytest.approx(-(2500 / 100.2) * 0.2)


def test_no_look_ahead_features_and_trades():
    rng = np.random.default_rng(1)
    logr = np.cumsum(rng.normal(0, 3, 400))
    m1 = _market(logr)
    alt = logr.copy()
    alt[250:] += rng.normal(0, 50, 150)       # futur différent à partir de 250
    m2 = _market(alt)
    f1 = pb.compute_features(m1.logr_bps, m1.sid, 30)
    f2 = pb.compute_features(m2.logr_bps, m2.sid, 30)
    for k in ("mean", "std", "z", "dev", "slope"):
        np.testing.assert_array_equal(f1[k][:250], f2[k][:250])
    p = _params(window=30, entry=5.0)
    t1 = pb.simulate(m1, f1, p, _cost(0.3, 0.2))
    t2 = pb.simulate(m2, f2, p, _cost(0.3, 0.2))
    # tout trade décidé et clos avant la modification est identique
    done1 = [t for t in t1 if t["exit_idx"] < 250]
    done2 = [t for t in t2 if t["exit_idx"] < 250]
    assert done1 and done1 == done2


def test_entry_executes_next_bar_not_signal_bar():
    logr = np.r_[np.zeros(15), 30.0, np.full(40, 30.0)]
    m = _market(logr)
    f = pb.compute_features(m.logr_bps, m.sid, 10)
    tr = pb.simulate(m, f, _params(), _cost())
    assert tr[0]["entry_idx"] == 16 and tr[0]["dir"] == -1   # signal à la clôture de 15, exécution à 16


def test_exit_on_target():
    logr = np.r_[np.zeros(15), 30.0, 30.0, np.zeros(30)]
    m = _market(logr)
    f = pb.compute_features(m.logr_bps, m.sid, 10)
    tr = pb.simulate(m, f, _params(), _cost())
    assert tr[0]["reason"] == "cible"
    assert tr[0]["exit_idx"] == 18        # retour constaté à la clôture de 17, sortie à 18
    assert tr[0]["pnl"] > 0               # vendu le ratio haut, racheté bas


def test_timeout_20_minutes():
    # le ratio reste au-dessus de la moyenne (rampe) : jamais de retour à la cible
    logr = np.r_[np.zeros(15), 30.0 + 3.0 * np.arange(60)]
    m = _market(logr)
    f = pb.compute_features(m.logr_bps, m.sid, 10)
    tr = pb.simulate(m, f, _params(window=10, entry=10.0), _cost())
    t = tr[0]
    assert t["reason"] == "timeout"
    assert t["hold_min"] == 20
    assert t["exit_idx"] - t["entry_idx"] == 20


def test_session_forced_close_and_no_late_entry():
    # séance de 60 min : sortie forcée 10 min avant la clôture, aucune entrée dans les 20 dernières
    n = 60
    logr = np.r_[np.zeros(15), 30.0 + 3.0 * np.arange(n - 15)]
    m = _market(logr)
    m.close_ms[:] = m.ts[-1] + MIN
    f = pb.compute_features(m.logr_bps, m.sid, 10)
    p = _params(hold_max=100)
    tr = pb.simulate(m, f, p, _cost())
    assert tr[0]["reason"] == "seance"
    assert tr[0]["exit_ts"] == m.close_ms[0] - 10 * MIN
    for t in tr:
        assert t["entry_ts"] < m.close_ms[0] - 20 * MIN


def test_positions_never_cross_a_gap():
    logr = np.r_[np.zeros(15), 30.0 + 3.0 * np.arange(45), np.zeros(60)]
    m = _market(logr, sessions=[60, 60])
    assert len(set(m.sid)) == 2
    f = pb.compute_features(m.logr_bps, m.sid, 10)
    tr = pb.simulate(m, f, _params(hold_max=100), _cost())
    for t in tr:
        assert m.sid[t["entry_idx"]] == m.sid[t["exit_idx"]]
        assert t["exit_ts"] <= m.close_ms[t["entry_idx"]] - 10 * MIN


def test_daily_stop_blocks_rest_of_session():
    # chaque trade perd : le ratio continue de s'écarter (rampe), coûts élevés
    up = 30.0 + 40.0 * np.arange(200)
    logr = np.r_[np.zeros(15), up]
    m = _market(logr)
    f = pb.compute_features(m.logr_bps, m.sid, 10)
    p = _params(daily_max_loss=100.0, hold_max=20)
    tr = pb.simulate(m, f, p, _cost(5.0, 5.0))
    assert tr[-1]["reason"] == "stop_jour"
    assert sum(t["pnl"] for t in tr) <= -100.0
    # avant le dernier trade, le cumul était au-dessus de -100 $
    assert sum(t["pnl"] for t in tr[:-1]) > -100.0


def test_derive_sessions_from_gaps():
    ts = np.r_[T0 + np.arange(5) * MIN, T0 + (5 + 60) * MIN + np.arange(5) * MIN]
    sid, close = pb.derive_sessions(ts)
    assert list(sid) == [0] * 5 + [1] * 5
    assert close[0] == ts[4] + MIN


def test_walk_forward_folds_do_not_overlap():
    folds = pb.walk_forward_folds(list(range(20)), 7, 3, 3)
    tests = [d for _, te in folds for d in te]
    assert len(tests) == len(set(tests))
    for tr, te in folds:
        assert max(tr) < min(te)


def test_two_tranches_for_2500_per_leg():
    m = _market(np.zeros(5))
    p = _params(leg_usd=2500.0)
    assert p.n_tranches == 2
    ent = pb.execute_pair(m, 1, +1, p, _cost(0.5, 0.25), closing=False)
    assert ent["vol"] == pytest.approx(5_000, rel=1e-3)
    ext = pb.execute_pair(m, 2, +1, p, _cost(0.5, 0.25), closing=True, qty=ent["q"])
    assert ent["cash"] + ext["cash"] == pytest.approx(-(ent["fees"] + ext["fees"]), rel=1e-9)


def test_cycle_alternates_and_reenters_same_minute_when_c0():
    m = _market(np.zeros(120))
    m.close_ms[:] = m.ts[-1] + MIN
    tr = pb.simulate(m, pb.compute_features(m.logr_bps, m.sid, 10),
                     _params(method="cycle", dir_mode="alt", hold_max=1, cooldown=0), _cost())
    assert [t["dir"] for t in tr[:4]] == [1, -1, 1, -1]
    assert tr[1]["entry_idx"] == tr[0]["exit_idx"]          # réouverture dans la minute de sortie
    assert all(t["reason"] == "timeout" for t in tr[:-1])
    # prix constants : coût = demi-spread + glissement uniquement
    tr2 = pb.simulate(m, pb.compute_features(m.logr_bps, m.sid, 10),
                      _params(method="cycle", hold_max=2, cooldown=5), _cost())
    assert tr2[1]["entry_idx"] == tr2[0]["exit_idx"] + 5


def test_cycle_z_direction_is_against_current_deviation():
    logr = np.r_[np.zeros(15), np.full(30, 30.0)]
    m = _market(logr)
    f = pb.compute_features(m.logr_bps, m.sid, 10)
    tr = pb.simulate(m, f, _params(method="cycle", dir_mode="z", hold_max=1, cooldown=1), _cost())
    assert tr[0]["dir"] == -1 and tr[0]["entry_idx"] == 16


def test_order_rate_cap():
    m = _market(np.zeros(200))
    m.close_ms[:] = m.ts[-1] + MIN
    f = pb.compute_features(m.logr_bps, m.sid, 10)
    tr = pb.simulate(m, f, _params(method="cycle", hold_max=1, cooldown=0, max_orders_h=80), _cost())
    ts = np.array([t["entry_ts"] for t in tr for _ in range(t["orders"] // 2)] +
                  [t["exit_ts"] for t in tr for _ in range(t["orders"] // 2)])
    for t0 in ts:
        assert ((ts > t0 - 3_600_000) & (ts <= t0)).sum() <= 80


def test_hour_filter_blocks_other_hours():
    logr = np.r_[np.zeros(15), np.full(200, 30.0)]
    m = _market(logr)
    f = pb.compute_features(m.logr_bps, m.sid, 10)
    tr = pb.simulate(m, f, _params(method="cycle", hold_max=1, cooldown=1, hours=(2,)), _cost())
    assert tr and all(t["entry_hour"] == 2 for t in tr)


def test_break_even_slippage():
    assert pb.break_even_slip(30.0, 130.0, 70.0) == pytest.approx(0.4)
    assert pb.break_even_slip(90.0, 190.0, 70.0) < 0
