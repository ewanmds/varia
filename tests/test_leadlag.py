import json

import numpy as np

from tools import leadlag


def synth(lag_ms=300, n_s=600, seed=1):
    """Var publie à 1 Hz, HL suit le même prix latent avec `lag_ms` de retard, toutes les 100 ms."""
    rng = np.random.default_rng(seed)
    t = np.arange(0, n_s * 1000, 50.0)
    latent = 100 * np.exp(np.cumsum(rng.normal(0, 2e-4, len(t))))
    tv = np.arange(1000, n_s * 1000 - 1000, 1000.0)
    pv = latent[np.searchsorted(t, tv, side="right") - 1]
    th = np.arange(1000, n_s * 1000 - 1000, 100.0)
    ph = latent[np.searchsorted(t, th - lag_ms, side="right") - 1]
    return tv, pv, th, ph


def test_xcorr_positive_lag_means_var_leads():
    tv, pv, th, ph = synth(lag_ms=300)
    xc = leadlag.xcorr(tv, pv, th, ph)
    assert xc["best_lag_ms"] == 300
    tv, pv, th, ph = synth(lag_ms=-400)  # HL en avance de 400 ms
    assert leadlag.xcorr(tv, pv, th, ph)["best_lag_ms"] == -400


def test_level_alignment_finds_the_lag():
    tv, pv, th, ph = synth(lag_ms=300)
    basis = leadlag.trailing_basis(tv, pv, th, ph)
    L, err = leadlag.level_alignment(tv, pv, th, ph, basis)
    assert 250 <= L <= 350


def test_var_first_event_hl_lagging_and_pnl():
    # Var monte de 10 pb à t=20 s ; HL ne suit qu'à t=20,8 s.
    tv = np.arange(0, 30_000, 1000.0)
    pv = np.where(tv < 20_000, 100.0, 100.1)
    th = np.arange(0, 30_000, 100.0)
    mid = np.where(th < 20_800, 100.0, 100.1)
    h = {"t": th, "mid": mid, "bid": mid - 0.001, "ask": mid + 0.001,
         "bsz": np.full(len(th), 100.0), "asz": np.full(len(th), 100.0)}
    V = {"t": tv, "px": pv}
    basis = leadlag.trailing_basis(tv, pv, th, mid)
    ev = leadlag.var_first_events(V, h, basis, X=5, Y=2.5, delays=(200, 500, 1000), fee_bps=0.9,
                                  var_hs_bps=0.5)
    assert len(ev) == 1
    e = ev[0]
    assert e["lag"] == {200: True, 500: True, 1000: False}
    assert 700 <= e["duration_ms"] <= 900
    edge, deep = e["trade"][200]
    assert 7 < edge < 9 and deep  # 10 pb - 0,1 demi-spread HL - 0,5 Var - 0,9 frais
    s = leadlag.summarize(ev, hours=1.0, delays=(200, 500, 1000))
    assert s["tr_200_per_h"] == 1 and s["tr_1000_per_h"] == 0


def test_load_and_report(tmp_path):
    tv, pv, th, ph = synth(lag_ms=300, n_s=200)
    with open(tmp_path / "var.jsonl", "w") as f:
        for t, p in zip(tv, pv):
            f.write(json.dumps({"t": int(t), "ts": int(t) - 200, "a": "USOILP", "px": p}) + "\n")
    with open(tmp_path / "hl.jsonl", "w") as f:
        for t, p in zip(th, ph):
            f.write(json.dumps({"t": int(t), "ts": int(t) - 400, "c": "xyz:CL", "k": "q",
                                "bid": p - 0.001, "bsz": 50, "ask": p + 0.001, "asz": 50}) + "\n")
    V, H = leadlag.load(tmp_path)
    r = leadlag.analyze_market(V["USOILP"], H["xyz:CL"], "xyz:CL", 0.5, 0.5)
    text = leadlag.render(tmp_path, {"USOILP": r}, 0.5, 0.5)
    assert "Var mène" in text
    assert "—" not in text
