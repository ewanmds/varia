"""Analyse avance/retard Var contre HL sur un enregistrement de tools/recorder.py.

Usage : python3 tools/leadlag.py data/recordings/<date-heure> [--var-half-spread-bps 0.5]
Écrit <dossier>/report.md (en français).

Séries utilisées :
- Var : prix du flux /prices (un seul prix par mise à jour, pas de bid/ask, environ 1 Hz) ;
- HL : milieu du meilleur prix `bbo` (bid, ask et tailles), poussé à chaque bloc où il change.
Base de temps par défaut : réception locale (ce que verrait le bot). La corrélation croisée est
aussi calculée sur les horodatages serveur.

Écart de référence (« basis ») : médiane glissante sur les 300 s précédentes de
log(prix Var) - log(milieu HL), mesurée à chaque mise à jour Var. Un marché est « en retard » quand
son prix, corrigé du basis, reste à plus de Y pb derrière celui de l'autre place.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import deque
from pathlib import Path
from typing import Optional

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

MARKETS = [("US100S", "xyz:XYZ100"), ("US500S", "xyz:SP500"), ("XAUS", "xyz:GOLD"),
           ("USOILP", "xyz:CL"), ("UKOILP", "xyz:BRENTOIL")]
HL_FEE_BPS = {"xyz:GOLD": 9.0}
HL_FEE_DEFAULT_BPS = 0.9
SIZE_USD = 500.0
MOVES_BPS = (3, 5, 10)
DELAYS_MS = (200, 500, 1000)
GRID_MS = 50
MAX_LAG_MS = 5000
BASIS_WINDOW_MS = 300_000
DURATION_CAP_MS = 10_000
HL_MOVE_WINDOW_MS = 1000
REFRACTORY_MS = 1000


# ---------------------------------------------------------------- chargement
def load(rec_dir: Path) -> tuple[dict, dict]:
    var: dict[str, list] = {}
    hl: dict[str, list] = {}
    with open(rec_dir / "var.jsonl") as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            var.setdefault(r["a"], []).append((r["t"], r.get("ts") or r["t"], r["px"]))
    with open(rec_dir / "hl.jsonl") as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("k") != "q" or r.get("bid") is None or r.get("ask") is None:
                continue
            hl.setdefault(r["c"], []).append((r["t"], r.get("ts") or r["t"], r["bid"], r["ask"],
                                              r.get("bsz") or 0.0, r.get("asz") or 0.0))
    V = {a: _arr(rows, ("t", "ts", "px")) for a, rows in var.items()}
    H = {c: _arr(rows, ("t", "ts", "bid", "ask", "bsz", "asz")) for c, rows in hl.items()}
    for h in H.values():
        h["mid"] = (h["bid"] + h["ask"]) / 2
    return V, H


def _arr(rows, names) -> dict:
    rows = sorted(rows, key=lambda r: r[0])
    a = np.array(rows, dtype=float) if rows else np.zeros((0, len(names)))
    return {n: a[:, i] for i, n in enumerate(names)}


def asof(t_src: np.ndarray, t_query) -> np.ndarray:
    """Indice de la dernière observation <= t_query (-1 si aucune)."""
    return np.searchsorted(t_src, t_query, side="right") - 1


# ---------------------------------------------------------------- corrélation croisée
def xcorr(tv, pv, th, ph, grid_ms: int = GRID_MS, max_lag_ms: int = MAX_LAG_MS) -> Optional[dict]:
    """Corrélation des rendements log, décalages de -max_lag à +max_lag par pas de `grid_ms`.

    Var ne publie qu'à ~1 Hz : sur une grille fixe, son rendement est daté de la fin d'un
    intervalle d'une seconde alors qu'il résume toute la seconde, ce qui biaise le pic d'environ
    -500 ms (HL semble mener). On échantillonne donc aux instants Var : pour chaque intervalle
    [tv(i-1), tv(i)], rendement Var contre rendement HL sur [tv(i-1) + L, tv(i) + L].
    Lag L > 0 : HL bouge APRÈS Var (Var mène). Renvoie {lags_ms, corr, best_lag_ms, best_corr, corr0}."""
    if len(tv) < 5 or len(th) < 5:
        return None
    ov = np.argsort(tv, kind="stable")
    oh = np.argsort(th, kind="stable")
    tv, pv, th, ph = tv[ov], pv[ov], th[oh], ph[oh]
    keep = (tv - max_lag_ms >= th[0]) & (tv + max_lag_ms <= th[-1])
    tv, pv = tv[keep], pv[keep]
    if len(tv) < 12:
        return None
    rv = np.diff(np.log(pv))
    lh = np.log(ph)
    lags, cs = [], []
    for L in range(-max_lag_ms, max_lag_ms + 1, grid_ms):
        lvl = lh[asof(th, tv + L)]
        rh = np.diff(lvl)
        if rv.std() == 0 or rh.std() == 0:
            c = float("nan")
        else:
            c = float(np.corrcoef(rv, rh)[0, 1])
        lags.append(L)
        cs.append(c)
    cs_arr = np.array(cs)
    if np.all(np.isnan(cs_arr)):
        return None
    i = int(np.nanargmax(cs_arr))
    return {"lags_ms": lags, "corr": cs, "best_lag_ms": lags[i], "best_corr": cs[i],
            "corr0": cs[len(cs) // 2]}


def level_alignment(tv, pv, th, ph, basis, step_ms: int = GRID_MS, max_lag_ms: int = MAX_LAG_MS):
    """Décalage L qui minimise la médiane de |log Var(t) - basis(t) - log HL(t + L)| aux instants
    des mises à jour Var. L > 0 : HL rejoint le niveau Var plus tard (Var mène)."""
    ok = ~np.isnan(basis)
    if ok.sum() < 20:
        return None
    tq, lv = tv[ok], np.log(pv[ok]) - basis[ok]
    best = None
    for L in range(-max_lag_ms, max_lag_ms + 1, step_ms):
        idx = asof(th, tq + L)
        good = idx >= 0
        if good.sum() < 20:
            continue
        err = float(np.median(np.abs(lv[good] - np.log(ph[idx[good]])))) * 1e4
        if best is None or err < best[1]:
            best = (L, err)
    return best


# ---------------------------------------------------------------- basis
def trailing_basis(tv, pv, th, ph, window_ms: int = BASIS_WINDOW_MS, min_n: int = 10) -> np.ndarray:
    """Médiane glissante (fenêtre passée) de log(Var) - log(HL milieu) aux instants Var."""
    idx = asof(th, tv)
    diff = np.where(idx >= 0, np.log(pv) - np.log(ph[np.maximum(idx, 0)]), np.nan)
    out = np.full(len(tv), np.nan)
    win: deque = deque()
    for i in range(len(tv)):
        while win and win[0][0] < tv[i] - window_ms:
            win.popleft()
        if len(win) >= min_n:
            out[i] = float(np.median([d for _, d in win]))
        if not np.isnan(diff[i]):
            win.append((tv[i], diff[i]))
    return out


def basis_at(tv, basis, t):
    i = asof(tv, t)
    return basis[i] if i >= 0 else float("nan")


# ---------------------------------------------------------------- exécution HL
def hl_exec_px(h, j: int, buy: bool, size_usd: float = SIZE_USD) -> tuple[float, bool]:
    """Prix taker sur le meilleur niveau HL ; second élément : la taille visible suffit-elle."""
    px = h["ask"][j] if buy else h["bid"][j]
    sz = h["asz"][j] if buy else h["bsz"][j]
    return px, sz * px >= size_usd


# ---------------------------------------------------------------- événements
def var_first_events(V, h, basis, X: float, Y: float, delays, fee_bps: float, var_hs_bps: float):
    """Var bouge de >= X pb entre deux mises à jour consécutives (moins de 3 s d'écart)."""
    tv, pv = V["t"], V["px"]
    th, mid = h["t"], h["mid"]
    ev = []
    for i in range(1, len(tv)):
        if tv[i] - tv[i - 1] > 3000:
            continue
        m = (math.log(pv[i]) - math.log(pv[i - 1])) * 1e4
        if abs(m) < X or np.isnan(basis[i]):
            continue
        s = 1.0 if m > 0 else -1.0
        b = basis[i]
        t0 = tv[i]

        def gap(t):
            j = asof(th, t)
            k = asof(tv, t)
            if j < 0:
                return float("nan"), -1, k
            return s * ((math.log(pv[k]) - b) - math.log(mid[j])) * 1e4, j, k

        g0, _, _ = gap(t0)
        e = {"t0": t0, "move_bps": m, "gap0": g0, "lag": {}, "trade": {}}
        for d in delays:
            g, j, k = gap(t0 + d)
            e["lag"][d] = g > Y
            if j >= 0:
                buy_hl = s > 0
                hl_px, deep = hl_exec_px(h, j, buy_hl)
                var_px = pv[k] * (1 - s * var_hs_bps / 1e4)
                edge = s * ((math.log(var_px) - b) - math.log(hl_px)) * 1e4 - fee_bps
                e["trade"][d] = (edge, deep)
        e["duration_ms"] = _duration(t0, g0, Y, lambda t: gap(t)[0], th, tv) if g0 > Y else None
        ev.append(e)
    return ev


def hl_first_events(V, h, tv_basis, basis, X: float, Y: float, delays, fee_bps: float,
                    var_hs_bps: float):
    """HL bouge de >= X pb sur la dernière seconde (une détection au plus par seconde)."""
    tv, pv = V["t"], V["px"]
    th, mid = h["t"], h["mid"]
    lm = np.log(mid)
    back = asof(th, th - HL_MOVE_WINDOW_MS)
    ev = []
    last_ev = -1e18
    for j in range(len(th)):
        if back[j] < 0 or th[j] - last_ev < REFRACTORY_MS:
            continue
        m = (lm[j] - lm[back[j]]) * 1e4
        if abs(m) < X:
            continue
        t0 = th[j]
        b = basis_at(tv_basis, basis, t0)
        if np.isnan(b) or asof(tv, t0) < 0:
            continue
        last_ev = t0
        s = 1.0 if m > 0 else -1.0

        def gap(t):
            jj = asof(th, t)
            k = asof(tv, t)
            return s * (math.log(mid[jj]) - (math.log(pv[k]) - b)) * 1e4, jj, k

        g0, _, _ = gap(t0)
        e = {"t0": t0, "move_bps": m, "gap0": g0, "lag": {}, "trade": {}}
        for d in delays:
            g, jj, k = gap(t0 + d)
            e["lag"][d] = g > Y
            sell_hl = s > 0
            hl_px, deep = hl_exec_px(h, jj, not sell_hl)
            var_px = pv[k] * (1 + s * var_hs_bps / 1e4)
            edge = s * (math.log(hl_px) - (math.log(var_px) - b)) * 1e4 - fee_bps
            e["trade"][d] = (edge, deep)
        e["duration_ms"] = _duration(t0, g0, Y, lambda t: gap(t)[0], th, tv) if g0 > Y else None
        ev.append(e)
    return ev


def _duration(t0, g0, Y, gapf, th, tv) -> float:
    """Temps écoulé jusqu'à ce que l'écart repasse sous Y (plafonné à DURATION_CAP_MS)."""
    t_end = t0 + DURATION_CAP_MS
    ts = np.concatenate([th[(th > t0) & (th <= t_end)], tv[(tv > t0) & (tv <= t_end)]])
    for t in np.sort(ts):
        if gapf(t) <= Y:
            return float(t - t0)
    return float(DURATION_CAP_MS)


def summarize(events, hours: float, delays) -> dict:
    out = {"n": len(events), "per_h": len(events) / hours if hours > 0 else float("nan")}
    lagged0 = [e for e in events if e["gap0"] > 0]
    out["gap0_med"] = float(np.median([e["gap0"] for e in events])) if events else float("nan")
    for d in delays:
        lag = [e for e in events if e["lag"].get(d)]
        out[f"lag_{d}_per_h"] = len(lag) / hours if hours > 0 else float("nan")
        out[f"lag_{d}_pct"] = 100.0 * len(lag) / len(events) if events else float("nan")
        tr = [e["trade"][d] for e in events if d in e["trade"] and e["trade"][d][0] > 0]
        out[f"tr_{d}_per_h"] = len(tr) / hours if hours > 0 else float("nan")
        out[f"pnl_{d}_per_h"] = sum(SIZE_USD * x / 1e4 for x, _ in tr) / hours if hours > 0 else float("nan")
        out[f"edge_{d}_avg"] = float(np.mean([x for x, _ in tr])) if tr else float("nan")
        out[f"shallow_{d}"] = sum(1 for _, deep in tr if not deep)
    durs = [e["duration_ms"] for e in events if e["duration_ms"] is not None]
    out["dur_med"] = float(np.median(durs)) if durs else float("nan")
    out["dur_p90"] = float(np.percentile(durs, 90)) if durs else float("nan")
    out["n_gap0"] = len(lagged0)
    return out


# ---------------------------------------------------------------- analyse d'un marché
def analyze_market(V, h, hl_coin: str, var_hs_bps: float, y_ratio: float) -> dict:
    fee = HL_FEE_BPS.get(hl_coin, HL_FEE_DEFAULT_BPS)
    t0 = max(V["t"][0], h["t"][0])
    t1 = min(V["t"][-1], h["t"][-1])
    hours = max(0.0, (t1 - t0) / 3.6e6)
    res = {"hours": hours, "fee": fee, "n_var": len(V["t"]), "n_hl": len(h["t"]),
           "var_delay_med": float(np.median(V["t"] - V["ts"])),
           "hl_delay_med": float(np.median(h["t"] - h["ts"])),
           "var_dt_med": float(np.median(np.diff(V["t"]))) if len(V["t"]) > 1 else float("nan"),
           "hl_dt_med": float(np.median(np.diff(h["t"]))) if len(h["t"]) > 1 else float("nan"),
           "hl_hs_med": float(np.median((h["ask"] - h["bid"]) / h["mid"] * 1e4 / 2))}
    res["xc_recv"] = xcorr(V["t"], V["px"], h["t"], h["mid"])
    ts_order = np.argsort(h["ts"], kind="stable")
    res["xc_server"] = xcorr(V["ts"], V["px"], h["ts"][ts_order], h["mid"][ts_order])
    basis = trailing_basis(V["t"], V["px"], h["t"], h["mid"])
    res["basis_med"] = float(np.nanmedian(basis) * 1e4) if np.any(~np.isnan(basis)) else float("nan")
    res["align"] = level_alignment(V["t"], V["px"], h["t"], h["mid"], basis)
    res["var_first"], res["hl_first"] = {}, {}
    for X in MOVES_BPS:
        Y = X * y_ratio
        vf = var_first_events(V, h, basis, X, Y, DELAYS_MS, fee, var_hs_bps)
        hf = hl_first_events(V, h, V["t"], basis, X, Y, DELAYS_MS, fee, var_hs_bps)
        res["var_first"][X] = summarize(vf, hours, DELAYS_MS)
        res["hl_first"][X] = summarize(hf, hours, DELAYS_MS)
    return res


# ---------------------------------------------------------------- rapport
def _f(x, nd=1):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "n/d"
    return f"{x:.{nd}f}"


def leader_text(xc) -> str:
    if xc is None:
        return "n/d"
    L = xc["best_lag_ms"]
    if L > 0:
        return f"Var mène de {L} ms"
    if L < 0:
        return f"HL mène de {-L} ms"
    return "simultané (0 ms)"


def render(rec_dir: Path, results: dict, var_hs_bps: float, y_ratio: float) -> str:
    L = [f"# Avance / retard Var contre HL", "",
         f"Enregistrement : `{rec_dir}`", "",
         "## Méthode", "",
         "- Var : flux websocket public `/prices` du front Omni. Il ne publie qu'un prix par "
         "instrument (pas de bid/ask), environ une fois par seconde. Ce prix sert de mid Var.",
         "- HL : meilleur bid/ask (`bbo`) du dex xyz, mis à jour à chaque bloc où il change. Mid = "
         "(bid + ask) / 2.",
         "- Base de temps : réception locale. La corrélation croisée est aussi donnée sur les "
         "horodatages serveur (horloge locale synchronisée NTP).",
         f"- Corrélation croisée des rendements du mid, décalages de -{MAX_LAG_MS // 1000} s à "
         f"+{MAX_LAG_MS // 1000} s par pas de {GRID_MS} ms. Comme Var ne publie qu'à 1 Hz, les "
         "rendements sont pris entre deux prix Var consécutifs, et le rendement HL sur le même "
         "intervalle décalé de L (une grille fixe de 50 ms biaiserait le pic d'environ 500 ms en "
         "faveur de HL). Décalage positif : HL bouge après Var, donc Var mène.",
         "- Alignement des niveaux : décalage qui minimise l'écart médian entre le prix Var corrigé "
         "du basis et le mid HL décalé. Moins sensible à la cadence de 1 Hz de Var.",
         "- Basis : médiane glissante sur 300 s de log(Var) - log(mid HL).",
         f"- Seuil de retard Y = {y_ratio:g} x X. « En retard après d ms » : l'autre place reste à "
         "plus de Y pb derrière, basis déduit.",
         "- Événement Var : écart d'au moins X pb entre deux prix Var consécutifs. Événement HL : "
         f"mouvement du mid d'au moins X pb sur {HL_MOVE_WINDOW_MS} ms (au plus un par seconde).",
         f"- PnL théorique pour {SIZE_USD:.0f} $ : Var d'abord, on prend HL en IOC au meilleur prix "
         "(demi-spread HL inclus) et on couvre sur Var au nouveau prix Var moins un demi-spread Var "
         f"de {var_hs_bps:g} pb (pas de bid/ask dans le flux). HL d'abord : on prend Var au prix "
         "en retard et on couvre HL en taker. Frais HL taker 0,9 pb (GOLD 9 pb). Seuls les "
         "événements dont l'écart net est positif au moment d'agir sont comptés. Sortie non "
         "comptée (convergence supposée gratuite vers le basis).", ""]
    L += ["## Synthèse", "",
          "| Marché | Durée (h) | MAJ Var | MAJ HL | Pic corr. (réception) | Pic corr. (serveur) | "
          "Alignement niveaux | Délai Var (ms) | Délai HL (ms) | Basis (pb) | Demi-spread HL (pb) |",
          "|---|---|---|---|---|---|---|---|---|---|---|"]
    for asset, coin in MARKETS:
        r = results.get(asset)
        if r is None:
            L.append(f"| {asset} / {coin} | pas de données | | | | | | | | | |")
            continue
        xr, xs, al = r["xc_recv"], r["xc_server"], r["align"]
        L.append(f"| {asset} / {coin} | {_f(r['hours'], 2)} | {r['n_var']} (1 / {_f(r['var_dt_med'], 0)} ms) "
                 f"| {r['n_hl']} (1 / {_f(r['hl_dt_med'], 0)} ms) "
                 f"| {leader_text(xr)} (r={_f(xr and xr['best_corr'], 2)}, r0={_f(xr and xr['corr0'], 2)}) "
                 f"| {leader_text(xs)} (r={_f(xs and xs['best_corr'], 2)}) "
                 f"| {'n/d' if al is None else ('HL suit de ' if al[0] >= 0 else 'Var suit de ') + str(abs(al[0])) + ' ms (' + _f(al[1], 2) + ' pb)'} "
                 f"| {_f(r['var_delay_med'], 0)} | {_f(r['hl_delay_med'], 0)} | {_f(r['basis_med'], 1)} "
                 f"| {_f(r['hl_hs_med'], 2)} |")
    L += ["", "Délai = réception locale moins horodatage serveur (médiane).", ""]
    for key, title, who in (("var_first", "Var bouge d'abord, HL en retard", "HL"),
                            ("hl_first", "HL bouge d'abord, Var en retard", "Var")):
        L += [f"## {title}", "",
              f"| Marché | X (pb) | Évén./h | Écart méd. à t0 (pb) | {who} en retard 200 / 500 / 1000 ms (/h) "
              f"| Durée méd. / p90 (ms) | Trades/h (200 / 500 / 1000 ms) | PnL $/h (200 / 500 / 1000 ms) "
              "| Écart net moyen (pb, 200 ms) | Taille visible insuffisante |",
              "|---|---|---|---|---|---|---|---|---|---|"]
        for asset, coin in MARKETS:
            r = results.get(asset)
            if r is None:
                continue
            for X in MOVES_BPS:
                s = r[key][X]
                L.append(
                    f"| {asset} | {X} | {_f(s['per_h'])} | {_f(s['gap0_med'])} "
                    f"| {' / '.join(_f(s[f'lag_{d}_per_h']) for d in DELAYS_MS)} "
                    f"| {_f(s['dur_med'], 0)} / {_f(s['dur_p90'], 0)} "
                    f"| {' / '.join(_f(s[f'tr_{d}_per_h']) for d in DELAYS_MS)} "
                    f"| {' / '.join(_f(s[f'pnl_{d}_per_h'], 2) for d in DELAYS_MS)} "
                    f"| {_f(s['edge_200_avg'], 2)} | {sum(s[f'shallow_{d}'] for d in DELAYS_MS)} |")
        L.append("")
    L += ["## Lecture", ""] + conclusions(results) + [""]
    return "\n".join(L)


def conclusions(results) -> list[str]:
    out = []
    for asset, coin in MARKETS:
        r = results.get(asset)
        if r is None:
            out.append(f"- {asset} : pas de données.")
            continue
        vf = r["var_first"][5]
        hf = r["hl_first"][5]
        out.append(
            f"- {asset} / {coin} : corrélation croisée {leader_text(r['xc_recv'])} en réception, "
            f"{leader_text(r['xc_server'])} en horodatage serveur. Mouvements de 5 pb : "
            f"{_f(vf['per_h'])}/h côté Var (HL encore en retard à 500 ms : {_f(vf['lag_500_per_h'])}/h, "
            f"PnL théorique {_f(vf['pnl_500_per_h'], 2)} $/h), {_f(hf['per_h'])}/h côté HL "
            f"(Var encore en retard à 500 ms : {_f(hf['lag_500_per_h'])}/h, PnL théorique "
            f"{_f(hf['pnl_500_per_h'], 2)} $/h).")
    out.append("- Le prix Var n'arrive qu'à 1 Hz : une avance de Var inférieure à la seconde est "
               "difficile à distinguer. La corrélation au décalage 0 et l'alignement des niveaux "
               "complètent le pic de corrélation.")
    return out


def main() -> None:
    p = argparse.ArgumentParser(description="Analyse avance/retard Var contre HL")
    p.add_argument("rec_dir")
    p.add_argument("--var-half-spread-bps", type=float, default=0.5)
    p.add_argument("--y-ratio", type=float, default=0.5, help="Y = ratio x X (défaut 0,5)")
    args = p.parse_args()
    rec = Path(args.rec_dir)
    V, H = load(rec)
    results = {}
    for asset, coin in MARKETS:
        if asset in V and coin in H and len(V[asset]["t"]) > 5 and len(H[coin]["t"]) > 5:
            results[asset] = analyze_market(V[asset], H[coin], coin, args.var_half_spread_bps, args.y_ratio)
    text = render(rec, results, args.var_half_spread_bps, args.y_ratio)
    (rec / "report.md").write_text(text)
    print(text)


if __name__ == "__main__":
    main()
