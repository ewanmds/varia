"""Backtest de retour à la moyenne sur le ratio WTI/Brent de Variational (USOILP / UKOILP).

Recherche uniquement : aucun ordre, aucun appel réseau. Entrées :
  data/candles/USOILP_1m.csv, data/candles/UKOILP_1m.csv (bougies 1 min, unix_time_ms = ouverture)
  data/spreads/spreads.csv (spreads Var mesurés, modèle de coût)
  data/recordings/<dossier>/var.jsonl (mid Var 1 Hz, contrôle intra-minute seulement)

Stratégie : r = log(P_USOILP / P_UKOILP). Quand r s'écarte trop de sa norme glissante, on vend le
gagnant et on achète le perdant (5 000 $ par jambe, 4 ordres de 2 500 $ espacés d'environ 1 s :
A, B, A, B). Sortie au retour à la cible ou au bout de 20 min, fermeture forcée 10 min avant la
clôture de séance, pas d'entrée dans les 20 dernières minutes, stop journalier de 100 $.

Anti biais : la décision prise à la clôture de la bougie t n'utilise que les bougies <= t (stats
glissantes calculées par séance, sur le passé), l'exécution se fait dans la bougie t+1 (prix
interpolé entre son ouverture et sa clôture à la seconde d'envoi de chaque tranche).

Usage :
  python3 tools/pair_backtest.py               grille complète + rapport data/backtest/pair_report.md
  python3 tools/pair_backtest.py --volume      volume max sous 70-80 $/M + data/backtest/volume_report.md
  python3 tools/pair_backtest.py --sanity-only contrôle 1 Hz seulement
"""
from __future__ import annotations

import argparse
import itertools
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
CANDLES = ROOT / "data" / "candles"
SPREADS = ROOT / "data" / "spreads" / "spreads.csv"
REC = ROOT / "data" / "recordings" / "20260923-165841" / "var.jsonl"
OUT = ROOT / "data" / "backtest"

MIN_MS = 60_000
WTI, BRENT = "USOILP", "UKOILP"


# ---------------------------------------------------------------- données et séances

def load_candles(dir_: Path = CANDLES) -> pd.DataFrame:
    """Minutes communes aux deux actifs : ts (ms, ouverture), ow, cw (WTI), ob, cb (Brent)."""
    a = pd.read_csv(dir_ / f"{WTI}_1m.csv", index_col="unix_time_ms")
    b = pd.read_csv(dir_ / f"{BRENT}_1m.csv", index_col="unix_time_ms")
    df = pd.DataFrame({"ow": a["open"], "cw": a["close"], "ob": b["open"], "cb": b["close"]}).dropna()
    df = df.sort_index()
    df.index.name = "ts"
    return df.reset_index()


def derive_sessions(ts: np.ndarray, close_hour_utc: int = 21) -> tuple[np.ndarray, np.ndarray]:
    """Découpe en séances sur les trous de données (> 1 min). Renvoie (id de séance par ligne,
    heure de clôture en ms par ligne). La clôture d'une séance complète est la fin de sa dernière
    bougie ; si la dernière séance est tronquée (fin des données avant l'heure de clôture habituelle),
    sa clôture est fixée à la prochaine close_hour_utc."""
    ts = np.asarray(ts, dtype=np.int64)
    brk = np.r_[True, np.diff(ts) != MIN_MS]
    sid = np.cumsum(brk) - 1
    close = np.empty_like(ts)
    for s in np.unique(sid):
        idx = np.flatnonzero(sid == s)
        end = int(ts[idx[-1]]) + MIN_MS
        t_end = pd.Timestamp(end, unit="ms", tz="UTC")
        if s == sid[-1] and not (t_end.hour == close_hour_utc and t_end.minute == 0):
            nxt = t_end.normalize() + pd.Timedelta(hours=close_hour_utc)
            if nxt < t_end:
                nxt += pd.Timedelta(days=1)
            end = int(nxt.value // 1_000_000)
        close[idx] = end
    return sid, close


# ---------------------------------------------------------------- coûts

@dataclass
class CostModel:
    """Demi-spread en pb par actif, éventuellement par heure UTC (clé -1 = médiane globale)."""
    half_bps: dict[str, dict[int, float]]
    source: str = ""

    def half(self, asset: str, hour: int) -> float:
        d = self.half_bps[asset]
        return d.get(hour, d[-1])


def load_cost_model(path: Path = SPREADS, sizes=(2500.0, 5000.0), min_per_hour: int = 20,
                    min_hours: int = 18) -> CostModel:
    """Demi-spread = spread complet / 2. Par heure si chaque heure retenue a assez de mesures et
    qu'assez d'heures sont couvertes, sinon médiane globale."""
    df = pd.read_csv(path)
    df = df[df["size_usd"].isin(sizes) & (df.get("status", 200) == 200)]
    df["hour"] = pd.to_datetime(df["ts_utc"]).dt.hour
    out: dict[str, dict[int, float]] = {}
    srcs = []
    for asset, g in df.groupby("asset"):
        d = {-1: float(g["spread_bps"].median()) / 2}
        cnt = g.groupby("hour")["spread_bps"].count()
        good = cnt[cnt >= min_per_hour]
        if len(good) >= min_hours:
            med = g.groupby("hour")["spread_bps"].median()
            for h in good.index:
                d[int(h)] = float(med[h]) / 2
            srcs.append(f"{asset} par heure ({len(g)} mesures)")
        else:
            srcs.append(f"{asset} médiane ({len(g)} mesures)")
        out[str(asset)] = d
    return CostModel(out, ", ".join(srcs))


def fill_price(mid: float, side: int, half_bps: float, slip_bps: float) -> float:
    """side +1 achat, -1 vente : on paie le demi-spread et le glissement."""
    return mid * (1.0 + side * (half_bps + slip_bps) / 1e4)


def intrabar_mid(o: float, c: float, sec: float) -> float:
    """Mid approché à la seconde sec de la bougie (interpolation linéaire ouverture -> clôture)."""
    return o + (c - o) * (sec / 60.0)


# ---------------------------------------------------------------- signal

@dataclass(frozen=True)
class Params:
    method: str = "z"          # "z" (z-score), "bps" (écart fixe en pb) ou "cycle" (volume sans signal)
    window: int = 60           # fenêtre glissante en minutes (par séance, sur le passé)
    entry: float = 2.0         # z_in ou écart d'entrée en pb
    exit: float = 0.0          # z_out ou écart de sortie en pb (exit < 0 : pas de sortie à la cible)
    slope_n: int = 30          # filtre anti-tendance : pente de la moyenne sur slope_n min
    slope_max: float = 0.0     # seuil de pente en pb (0 = filtre coupé)
    z_extreme: float = 0.0     # pas d'entrée si |z| > z_extreme (0 = filtre coupé)
    cooldown: int = 0          # minutes sans entrée après une sortie
    hold_max: int = 20
    dir_mode: str = "alt"      # famille cycle : "alt" (alterné) ou "z" (contre le signe du z courant)
    hours: tuple = ()          # heures UTC d'entrée autorisées (vide = toutes)
    close_flat_min: int = 10   # fermeture forcée N min avant la clôture
    no_entry_min: int = 20     # pas d'entrée dans les N dernières minutes
    leg_usd: float = 5000.0
    tranche_usd: float = 2500.0
    daily_max_loss: float = 100.0
    max_orders_h: int = 600    # plafond d'ordres sur toute heure glissante
    slip_bps: float = 0.5
    delay_s: float = 2.0       # délai entre la clôture de la bougie signal et le 1er ordre
    spacing_s: float = 1.5     # espacement des tranches (1 ordre / 1,5 s)

    @property
    def n_tranches(self) -> int:
        """Ordres par côté : 4 pour 5 000 $ par jambe (A, B, A, B), 2 pour 2 500 $ (A, B)."""
        return int(round(2 * self.leg_usd / self.tranche_usd))

    def label(self) -> str:
        if self.method == "cycle":
            s = f"cycle {self.dir_mode}" + (f" W{self.window}" if self.dir_mode == "z" else "")
            s += f" H{self.hold_max} C{self.cooldown}"
        else:
            unit = "z" if self.method == "z" else "pb"
            s = f"{self.method} W{self.window} in{self.entry:g}{unit}"
            s += f" out{self.exit:g}" if self.exit >= 0 else " sans cible"
            if self.hold_max != 20:
                s += f" H{self.hold_max}"
            if self.slope_max:
                s += f" pente<{self.slope_max:g}pb/{self.slope_n}"
            if self.z_extreme:
                s += f" |z|<{self.z_extreme:g}"
            if self.cooldown:
                s += f" cd{self.cooldown}"
        if self.hours:
            s += " h" + ",".join(str(h) for h in self.hours)
        return s + f" {self.leg_usd / 1000:g}k"


def compute_features(logr_bps: np.ndarray, sid: np.ndarray, window: int, slope_n: int = 30) -> dict:
    """Moyenne, écart-type, z, écart et pente glissants, calculés séance par séance et sur les
    seules bougies <= t (aucune donnée future, réinitialisés à chaque ouverture)."""
    s = pd.Series(logr_bps)
    g = s.groupby(sid)
    mean = g.transform(lambda x: x.rolling(window, min_periods=window).mean())
    std = g.transform(lambda x: x.rolling(window, min_periods=window).std())
    dev = s - mean
    z = dev / std.replace(0, np.nan)
    slope = mean - mean.groupby(sid).shift(slope_n)
    return {"mean": mean.to_numpy(), "std": std.to_numpy(), "dev": dev.to_numpy(),
            "z": z.to_numpy(), "slope": slope.to_numpy()}


def entry_signal(feat: dict, p: Params) -> np.ndarray:
    """+1 = acheter le ratio (long WTI, short Brent), -1 = vendre le ratio, 0 = rien. Pour la famille
    cycle en mode alterné, renvoie 1 partout (la direction est choisie pendant la simulation)."""
    if p.method == "cycle":
        if p.dir_mode == "alt":
            return np.ones(len(feat["z"]), dtype=np.int8)
        z = feat["z"]
        sig = np.where(z > 0, -1, np.where(z < 0, 1, 0))
        sig[np.isnan(z)] = 0
        return sig.astype(np.int8)
    x = feat["z"] if p.method == "z" else feat["dev"]
    sig = np.where(x >= p.entry, -1, np.where(x <= -p.entry, 1, 0))
    sig[np.isnan(x)] = 0
    if p.slope_max:
        sl = np.abs(feat["slope"])
        sig[~(sl <= p.slope_max)] = 0
    if p.z_extreme:
        sig[~(np.abs(feat["z"]) <= p.z_extreme)] = 0
    return sig.astype(np.int8)


def exit_target(feat: dict, p: Params, direction: int, i: int) -> bool:
    if p.method == "cycle" or p.exit < 0:
        return False
    x = feat["z"][i] if p.method == "z" else feat["dev"][i]
    if np.isnan(x):
        return False
    return x <= p.exit if direction == -1 else x >= -p.exit


# ---------------------------------------------------------------- simulation

@dataclass
class Market:
    ts: np.ndarray
    ow: np.ndarray
    cw: np.ndarray
    ob: np.ndarray
    cb: np.ndarray
    sid: np.ndarray
    close_ms: np.ndarray
    hour: np.ndarray = field(init=False)

    def __post_init__(self):
        self.hour = ((self.ts // 3_600_000) % 24).astype(int)

    @classmethod
    def from_df(cls, df: pd.DataFrame) -> "Market":
        sid, close = derive_sessions(df["ts"].to_numpy())
        return cls(df["ts"].to_numpy(np.int64), df["ow"].to_numpy(float), df["cw"].to_numpy(float),
                   df["ob"].to_numpy(float), df["cb"].to_numpy(float), sid, close)

    @property
    def logr_bps(self) -> np.ndarray:
        return np.log(self.cw / self.cb) * 1e4


def execute_pair(m: Market, i: int, direction: int, p: Params, cost: CostModel, closing: bool,
                 qty: tuple[float, float] | None = None, offset_s: float = 0.0) -> dict:
    """Tranches alternées A, B, A, B... (A = WTI) envoyées pendant la bougie i, aux secondes
    offset + delay + k * spacing. Ouverture : tranche_usd de nominal par tranche. Fermeture : la
    quantité de chaque jambe répartie à parts égales sur ses tranches. Le déséquilibre entre jambes
    vient des prix différents à chaque seconde d'envoi."""
    sides = {WTI: direction, BRENT: -direction}
    if closing:
        sides = {k: -v for k, v in sides.items()}
    nt = p.n_tranches
    per_leg = nt // 2
    q = {WTI: 0.0, BRENT: 0.0}
    cash = 0.0       # flux de trésorerie (+ vente, - achat)
    vol = 0.0
    fees = 0.0       # coût de demi-spread et glissement en $
    drift = 0.0      # écart mid de tranche vs mid d'ouverture de bougie (déséquilibre), en $
    h = int(m.hour[i])
    for k in range(nt):
        a = WTI if k % 2 == 0 else BRENT
        sec = min(offset_s + p.delay_s + k * p.spacing_s, 59.0)
        o, c = (m.ow[i], m.cw[i]) if a == WTI else (m.ob[i], m.cb[i])
        mid = intrabar_mid(o, c, sec)
        side = sides[a]
        px = fill_price(mid, side, cost.half(a, h), p.slip_bps)
        n = (qty[0] if a == WTI else qty[1]) / per_leg if closing else p.tranche_usd / px
        q[a] += n
        cash -= side * n * px
        vol += n * px
        fees += n * abs(px - mid)
        drift += -side * n * (mid - o)
    return {"q": (q[WTI], q[BRENT]), "cash": cash, "vol": vol, "fees": fees, "drift": drift,
            "end_s": offset_s + p.delay_s + nt * p.spacing_s}


def mark_pnl(m: Market, i: int, direction: int, qty: tuple[float, float], entry_cash: float) -> float:
    """PnL latent à la clôture de la bougie i (valorisation au mid, sans frais de sortie)."""
    val = direction * qty[0] * m.cw[i] - direction * qty[1] * m.cb[i]
    return entry_cash + val


def simulate(m: Market, feat: dict, p: Params, cost: CostModel) -> list[dict]:
    """Une paire à la fois. Décision à la clôture de la bougie i, exécution dans la bougie i+1.

    Familles à signal : la décision suivante se prend au plus tôt à la clôture de la bougie de sortie
    (+ pause). Famille cycle : réouverture C minutes après la sortie ; avec C = 0, dans la même minute
    juste après les ordres de sortie (décision sur la dernière bougie close)."""
    sig = entry_signal(feat, p)
    nxt_ok = np.r_[(m.sid[1:] == m.sid[:-1]) & (np.diff(m.ts) == MIN_MS), False]
    exec_ts = np.r_[m.ts[1:], m.ts[-1] + MIN_MS]
    can_enter = nxt_ok & (exec_ts < m.close_ms - p.no_entry_min * MIN_MS)
    if p.hours:
        exec_hour = np.r_[m.hour[1:], m.hour[-1]]
        can_enter &= np.isin(exec_hour, list(p.hours))
    cand = np.flatnonzero((sig != 0) & can_enter)
    trades: list[dict] = []
    day_pnl: dict[int, float] = {}
    stopped: set[int] = set()
    first_of_sid = {int(s): int(np.argmax(m.sid == s)) for s in np.unique(m.sid)}
    orders: list[int] = []        # horodatage (ms) de chaque ordre, pour le plafond horaire
    o_lo = 0
    nt = p.n_tranches
    last_dir = -1
    ptr, reentry_bar, reentry_off = 0, -1, 0.0
    while True:
        k = np.searchsorted(cand, ptr)
        if k >= len(cand):
            break
        i = int(cand[k])
        s = int(m.sid[i])
        if s in stopped:
            later = [first_of_sid[x] for x in first_of_sid if x > s]
            if not later:
                break
            ptr = min(later)
            continue
        e = i + 1
        # plafond d'ordres : ouverture + fermeture doivent tenir dans l'heure glissante
        t_e = int(m.ts[e])
        while o_lo < len(orders) and orders[o_lo] <= t_e - 3_600_000:
            o_lo += 1
        if len(orders) - o_lo + 2 * nt > p.max_orders_h:
            ptr = i + 1
            continue
        if p.method == "cycle" and p.dir_mode == "alt":
            d = -last_dir
        else:
            d = int(sig[i])
        last_dir = d
        off = reentry_off if e == reentry_bar else 0.0
        ent = execute_pair(m, e, d, p, cost, closing=False, offset_s=off)
        reason, j = None, e
        while True:
            realized = day_pnl.get(s, 0.0)
            unreal = mark_pnl(m, j, d, ent["q"], ent["cash"])
            if not nxt_ok[j]:
                reason = "fin_donnees"
                break
            if realized + unreal <= -p.daily_max_loss:
                reason = "stop_jour"
                break
            if m.ts[j + 1] >= m.close_ms[j] - p.close_flat_min * MIN_MS:
                reason = "seance"
                break
            if exit_target(feat, p, d, j):
                reason = "cible"
                break
            if (j + 1) - e >= p.hold_max:
                reason = "timeout"
                break
            j += 1
        if reason == "fin_donnees":
            # pas de bougie suivante dans la séance : sortie au mid de clôture de j avec frais
            ext = _exit_at_close(m, j, d, ent["q"], p, cost)
            ext["end_s"] = 60.0
            x_exec = j
        else:
            x_exec = j + 1
            ext = execute_pair(m, x_exec, d, p, cost, closing=True, qty=ent["q"])
        orders += [t_e] * nt + [int(m.ts[x_exec])] * nt
        pnl = ent["cash"] + ext["cash"]   # mêmes quantités à l'ouverture et à la fermeture
        vol = ent["vol"] + ext["vol"]
        tr = {"sid": s, "entry_idx": e, "exit_idx": x_exec, "entry_ts": t_e,
              "exit_ts": int(m.ts[x_exec]), "dir": d, "hold_min": x_exec - e, "reason": reason,
              "pnl": pnl, "fees": ent["fees"] + ext["fees"], "drift": ent["drift"] + ext["drift"],
              "vol": vol, "entry_hour": int(m.hour[e]), "orders": 2 * nt}
        trades.append(tr)
        day_pnl[s] = day_pnl.get(s, 0.0) + pnl
        if day_pnl[s] <= -p.daily_max_loss:
            stopped.add(s)
        if p.method == "cycle":
            # réouverture à la bougie x_exec + C (décision sur la bougie précédente, déjà close)
            ptr = x_exec - 1 + max(p.cooldown, 0)
            reentry_bar, reentry_off = (x_exec, ext["end_s"]) if p.cooldown == 0 else (-1, 0.0)
            if p.cooldown == 0 and ext["end_s"] + p.delay_s + nt * p.spacing_s > 59.0:
                ptr = x_exec        # plus la place dans la minute : bougie suivante
        else:
            # la prochaine décision se prend au plus tôt à la clôture de la bougie de sortie
            ptr = x_exec + max(p.cooldown, 0)
    return trades


def _exit_at_close(m: Market, i: int, direction: int, qty, p: Params, cost: CostModel) -> dict:
    h = int(m.hour[i])
    cash = vol = fees = 0.0
    for a, q, side, mid in ((WTI, qty[0], -direction, m.cw[i]), (BRENT, qty[1], direction, m.cb[i])):
        px = fill_price(mid, side, cost.half(a, h), p.slip_bps)
        cash -= side * q * px
        vol += q * px
        fees += q * abs(px - mid)
    return {"cash": cash, "vol": vol, "fees": fees, "drift": 0.0}


# ---------------------------------------------------------------- métriques

def metrics(trades: list[dict], days: list[int]) -> dict:
    """Agrégats sur un ensemble de séances (days = ids de séance, jours sans trade compris)."""
    dayset = set(days)
    t = [x for x in trades if x["sid"] in dayset]
    nd = max(len(days), 1)
    pnl = np.array([x["pnl"] for x in t]) if t else np.zeros(0)
    vol = float(sum(x["vol"] for x in t))
    daily = pd.Series(0.0, index=sorted(dayset))
    for x in t:
        daily[x["sid"]] += x["pnl"]
    eq = np.cumsum(pnl) if len(pnl) else np.zeros(1)
    dd = float(np.max(np.maximum.accumulate(np.r_[0, eq]) - np.r_[0, eq])) if len(pnl) else 0.0
    wins, losses = pnl[pnl > 0], pnl[pnl <= 0]
    reasons = pd.Series([x["reason"] for x in t]).value_counts(normalize=True).to_dict() if t else {}
    sd = float(daily.std(ddof=1)) if len(daily) > 1 else 0.0
    return {
        "n": len(t), "days": nd, "pnl": float(pnl.sum()), "vol": vol,
        "pnl_per_m": float(pnl.sum() / vol * 1e6) if vol else 0.0,
        "vol_day": vol / nd, "cycles_day": len(t) / nd,
        "hit": float((pnl > 0).mean()) if len(pnl) else 0.0,
        "avg_win": float(wins.mean()) if len(wins) else 0.0,
        "avg_loss": float(losses.mean()) if len(losses) else 0.0,
        "worst_trade": float(pnl.min()) if len(pnl) else 0.0,
        "worst_day": float(daily.min()) if len(daily) else 0.0,
        "max_dd": dd,
        "sharpe_d": float(daily.mean() / sd) if sd > 0 else 0.0,
        "pct_cible": reasons.get("cible", 0.0), "pct_timeout": reasons.get("timeout", 0.0),
        "pct_seance": reasons.get("seance", 0.0) + reasons.get("fin_donnees", 0.0),
        "pct_stop": reasons.get("stop_jour", 0.0),
        "fees": float(sum(x["fees"] for x in t)), "drift": float(sum(x["drift"] for x in t)),
        "cost_per_m": -float(pnl.sum() / vol * 1e6) if vol else 0.0,
        "orders_day": sum(x.get("orders", 8) for x in t) / nd,
        "peak_orders_h": _peak_orders_hour(t),
        "stop_days": len({x["sid"] for x in t if x["reason"] == "stop_jour"}),
    }


def _peak_orders_hour(t: list[dict]) -> int:
    """Nombre maximal d'ordres sur une heure calendaire (ouvertures et fermetures)."""
    if not t:
        return 0
    c: dict[int, int] = {}
    for x in t:
        half = x.get("orders", 8) // 2
        for ts in (x["entry_ts"], x["exit_ts"]):
            c[ts // 3_600_000] = c.get(ts // 3_600_000, 0) + half
    return max(c.values())


def break_even_slip(cost0: float, cost1: float, cap: float) -> float:
    """Glissement par ordre (pb) auquel le coût atteint cap $/M, par interpolation linéaire entre
    les coûts mesurés à 0 et 1 pb (le coût est linéaire en glissement : 100 $/M par pb). Négatif =
    déjà au-dessus du plafond sans glissement."""
    if cost1 == cost0:
        return float("inf") if cost0 <= cap else -1.0
    return (cap - cost0) / (cost1 - cost0)


# ---------------------------------------------------------------- grille, découpages

def build_grid() -> list[Params]:
    grid = []
    filters = [(0.0, 0.0), (3.0, 0.0), (0.0, 4.0), (3.0, 4.0)]   # (pente max pb/30 min, |z| max)
    for w in (30, 60, 120, 240):
        for (sl, zx), cd in itertools.product(filters, (0, 5)):
            for zi, zo in itertools.product((1.5, 2.0, 2.5, 3.0), (0.0, 0.5, 1.0)):
                grid.append(Params("z", w, zi, zo, slope_max=sl, z_extreme=zx, cooldown=cd))
            for bi, bo in itertools.product((10.0, 15.0, 20.0, 30.0), (0.0, 5.0)):
                grid.append(Params("bps", w, bi, bo, slope_max=sl, z_extreme=zx, cooldown=cd))
    return grid


def split_days(sids: list[int], frac: float = 0.6) -> tuple[list[int], list[int]]:
    k = int(round(len(sids) * frac))
    return sids[:k], sids[k:]


def walk_forward_folds(sids: list[int], train: int = 7, test: int = 3, step: int = 3):
    folds, start = [], 0
    while start + train < len(sids):
        folds.append((sids[start:start + train], sids[start + train:start + train + test]))
        start += step
    return folds


def select_best(results: dict, days: list[int], min_trades: int) -> Params | None:
    best, best_v = None, -np.inf
    for p, tr in results.items():
        mt = metrics(tr, days)
        if mt["n"] >= min_trades and mt["pnl_per_m"] > best_v:
            best, best_v = p, mt["pnl_per_m"]
    return best


# ---------------------------------------------------------------- contrôle 1 Hz

def sanity_1hz(df: pd.DataFrame, path: Path = REC, delay_s: float = 2.0) -> dict:
    """Compare, sur l'enregistrement 1 Hz, le mid réel aux secondes d'envoi des tranches avec
    l'interpolation linéaire ouverture -> clôture de la bougie utilisée par la simulation."""
    if not path.exists():
        return {}
    rows = [json.loads(x) for x in path.open()]
    r = pd.DataFrame(rows)
    r = r[r["a"].isin([WTI, BRENT])]
    out = {}
    cand = df.set_index("ts")
    for a, (oc, cc) in ((WTI, ("ow", "cw")), (BRENT, ("ob", "cb"))):
        s = r[r["a"] == a].set_index(pd.to_datetime(r.loc[r["a"] == a, "ts"], unit="ms"))["px"]
        s = s[~s.index.duplicated()].resample("1s").last().ffill()
        errs, moves = [], []
        for mts in range(int(s.index[0].value // 1e6) // MIN_MS * MIN_MS + MIN_MS,
                         int(s.index[-1].value // 1e6) - MIN_MS, MIN_MS):
            if mts not in cand.index:
                continue
            o, c = cand.loc[mts, oc], cand.loc[mts, cc]
            t0 = pd.Timestamp(mts, unit="ms")
            for k in range(4):
                sec = delay_s + k
                real = s.asof(t0 + pd.Timedelta(seconds=sec))
                errs.append((real / intrabar_mid(o, c, sec) - 1) * 1e4)
            moves.append((s.asof(t0 + pd.Timedelta(seconds=delay_s + 3)) / s.asof(t0 + pd.Timedelta(seconds=delay_s)) - 1) * 1e4)
        e, mv = np.array(errs), np.array(moves)
        out[a] = {"n_min": len(mv), "err_mean_bps": float(np.nanmean(e)), "err_std_bps": float(np.nanstd(e)),
                  "move3s_std_bps": float(np.nanstd(mv)), "move3s_p95_bps": float(np.nanpercentile(np.abs(mv), 95))}
    return out


# ---------------------------------------------------------------- rapport

def fmt(x: float, d: int = 1) -> str:
    return f"{x:,.{d}f}".replace(",", " ").replace(".", ",")


def pct(x: float) -> str:
    return f"{x * 100:.0f} %"


def _day_label(m: Market, s: int) -> str:
    end = int(m.close_ms[np.argmax(m.sid == s)])
    return pd.Timestamp(end, unit="ms", tz="UTC").strftime("%d/%m")


def run(args) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    df = load_candles()
    m = Market.from_df(df)
    cost = load_cost_model()
    sids = sorted(int(s) for s in np.unique(m.sid))
    # la dernière séance tronquée (en cours) reste incluse : elle compte comme un jour partiel
    is_days, oos_days = split_days(sids, 0.6)
    grid = build_grid()
    logr = m.logr_bps
    feats = {w: compute_features(logr, m.sid, w) for w in (30, 60, 120, 240)}

    res: dict[float, dict[Params, list]] = {}
    for slip in (0.0, 0.5, 1.0):
        res[slip] = {}
        for p in grid:
            q = Params(**{**asdict(p), "slip_bps": slip})
            res[slip][p] = simulate(m, feats[p.window], q, cost)
    base = res[0.5]

    min_is = max(30, 2 * len(is_days))
    rows = []
    for p, tr in base.items():
        a, b = metrics(tr, is_days), metrics(tr, oos_days)
        rows.append((p, a, b))
    eligible = [r for r in rows if r[1]["n"] >= min_is]
    by_is = dedupe(sorted(eligible, key=lambda r: -r[1]["pnl_per_m"]))
    chosen = by_is[0][0]
    by_oos = dedupe(sorted([r for r in rows if r[2]["n"] >= 20], key=lambda r: -r[2]["pnl_per_m"]))

    folds = walk_forward_folds(sids, 7, 3, 3)
    wf_rows, wf_trades, wf_test_days = [], [], []
    fixed_trades = base[chosen]
    for tr_days, te_days in folds:
        bp = select_best(base, tr_days, min_trades=14)
        if bp is None:
            continue
        a, b = metrics(base[bp], tr_days), metrics(base[bp], te_days)
        fx = metrics(fixed_trades, te_days)
        wf_rows.append((tr_days, te_days, bp, a, b, fx))
        wf_test_days += te_days            # plis de test sans recouvrement (pas = taille du test)
        wf_trades += [t for t in base[bp] if t["sid"] in set(te_days)]
    wf_all = metrics(wf_trades, wf_test_days)
    wf_fixed = metrics(fixed_trades, wf_test_days)
    sanity = sanity_1hz(df)

    _plots(m, chosen, base[chosen], is_days, oos_days, wf_trades, cost)
    _write_report(m, cost, sids, is_days, oos_days, grid, res, rows, by_is, by_oos, chosen, min_is,
                  folds, wf_rows, wf_all, wf_fixed, sanity)
    print(f"choisie : {chosen.label()}")
    print(f"rapport : {OUT / 'pair_report.md'}")


def _plots(m, chosen, trades, is_days, oos_days, wf_trades, cost):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    t = pd.DataFrame(trades)
    t["time"] = pd.to_datetime(t["exit_ts"], unit="ms", utc=True)
    fig, ax = plt.subplots(figsize=(12, 5))
    ax.plot(t["time"], t["pnl"].cumsum(), color="#1f6feb", lw=1.4, label=f"config retenue : {chosen.label()}")
    if wf_trades:
        w = pd.DataFrame(wf_trades).sort_values("exit_ts")
        ax.plot(pd.to_datetime(w["exit_ts"], unit="ms", utc=True), w["pnl"].cumsum(), color="#d1242f",
                lw=1.2, label="walk-forward (config re-choisie à chaque pli, test seulement)")
    split = pd.Timestamp(int(m.ts[np.argmax(m.sid == oos_days[0])]), unit="ms", tz="UTC")
    ax.axvline(split, color="#555", ls="--", lw=1)
    ax.text(split, ax.get_ylim()[1], "  hors échantillon ->", va="top", fontsize=9, color="#555")
    ax.axhline(0, color="#999", lw=0.8)
    import matplotlib.dates as mdates
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d/%m"))
    ax.set_title("PnL cumulé net (après spread et 0,5 pb de glissement par ordre), en $")
    ax.set_ylabel("$")
    ax.legend(loc="upper left", fontsize=9)
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(OUT / "pair_equity.png", dpi=110)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(12, 4.5))
    h = t.groupby("entry_hour")["pnl"].agg(["sum", "count"]).reindex(range(24), fill_value=0)
    ax.bar(h.index, h["sum"], color=np.where(h["sum"] >= 0, "#1f883d", "#d1242f"))
    for x, (s, c) in h.iterrows():
        if c:
            ax.text(x, s, f"{int(c)}", ha="center", va="bottom" if s >= 0 else "top", fontsize=7, color="#555")
    ax.set_xticks(range(24))
    ax.set_xlabel("heure UTC d'entrée")
    ax.set_ylabel("PnL net $")
    ax.set_title(f"PnL net par heure d'entrée, toute la période (nombre de trades au-dessus) : {chosen.label()}")
    ax.grid(alpha=0.3, axis="y")
    fig.tight_layout()
    fig.savefig(OUT / "pair_pnl_by_hour.png", dpi=110)
    plt.close(fig)


def overfit_flag(is_pm: float, oos_pm: float) -> str:
    """Écart IS/OOS : « oui » si un IS positif devient négatif ou perd plus de la moitié hors échantillon."""
    if is_pm <= 0:
        return "IS déjà < 0"
    return "oui" if oos_pm < 0.5 * is_pm else "non"


def gross_per_m(mt: dict) -> float:
    """PnL/M avant spread et glissement (l'effet du décalage entre tranches reste inclus)."""
    return (mt["pnl"] + mt["fees"]) / mt["vol"] * 1e6 if mt["vol"] else 0.0


def dedupe(rows: list) -> list:
    """Retire les configurations dont les trades sont identiques à une précédente (filtre sans effet)."""
    seen, out = set(), []
    for r in rows:
        key = (round(r[1]["pnl"], 6), r[1]["n"], round(r[2]["pnl"], 6), r[2]["n"])
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def variance_ratios(m: "Market", ks=(5, 20, 60)) -> dict:
    """Var(r_t+k - r_t) / (k Var(r_t+1 - r_t)) dans les séances : < 1 = retour à la moyenne."""
    lr = pd.Series(m.logr_bps)
    one = lr.groupby(m.sid).diff().var()
    return {k: float(lr.groupby(m.sid).diff(k).var() / (k * one)) for k in ks}


def _row(p: Params, a: dict, b: dict) -> str:
    gap = overfit_flag(a["pnl_per_m"], b["pnl_per_m"])
    return (f"| {p.label()} | {fmt(a['pnl_per_m'])} | {a['n']} | {fmt(b['pnl_per_m'])} | {fmt(gross_per_m(b))} | {fmt(b['pnl'], 0)} | "
            f"{b['n']} | {fmt(b['vol_day'] / 1000, 0)} k | {pct(b['hit'])} | {fmt(b['worst_day'], 0)} | "
            f"{pct(b['pct_timeout'])} | {gap} |")


HDR = ("| Config | IS PnL/M | IS trades | OOS PnL/M | OOS PnL/M brut (sans coût) | OOS PnL $ | OOS trades | OOS vol/jour | "
       "OOS gagnants | OOS pire jour $ | OOS sorties timeout | surapprentissage |\n|---|---|---|---|---|---|---|---|---|---|---|---|")


def _mt_table(cols: list[tuple[str, dict]]) -> str:
    keys = [("PnL net $", "pnl", 0), ("PnL/M $", "pnl_per_m", 1), ("trades", "n", 0), ("séances", "days", 0),
            ("volume/jour $", "vol_day", 0), ("cycles/jour", "cycles_day", 1), ("taux de gain", "hit", None),
            ("gain moyen $", "avg_win", 2), ("perte moyenne $", "avg_loss", 2), ("pire trade $", "worst_trade", 2),
            ("pire jour $", "worst_day", 1), ("drawdown max $", "max_dd", 1), ("Sharpe journalier (moy/écart)", "sharpe_d", 2),
            ("sorties cible", "pct_cible", None), ("sorties timeout 20 min", "pct_timeout", None),
            ("sorties fin de séance", "pct_seance", None), ("sorties stop jour", "pct_stop", None),
            ("PnL/M brut avant coûts $", "gross_pm", 1), ("coût spread + glissement $", "fees", 0), ("effet du décalage entre tranches $", "drift", 1)]
    s = "| | " + " | ".join(c for c, _ in cols) + " |\n|---|" + "---|" * len(cols) + "\n"
    for _, mt in cols:
        mt["gross_pm"] = gross_per_m(mt)
    for lab, k, d in keys:
        vals = [pct(mt[k]) if d is None else (str(mt[k]) if k in ("n", "days") else fmt(mt[k], d)) for _, mt in cols]
        s += f"| {lab} | " + " | ".join(vals) + " |\n"
    return s


def _write_report(m, cost, sids, is_days, oos_days, grid, res, rows, by_is, by_oos, chosen, min_is,
                  folds, wf_rows, wf_all, wf_fixed, sanity):
    base = res[0.5]
    a_is, a_oos = metrics(base[chosen], is_days), metrics(base[chosen], oos_days)
    a_all = metrics(base[chosen], sids)
    first = pd.Timestamp(int(m.ts[0]), unit="ms", tz="UTC")
    last = pd.Timestamp(int(m.ts[-1]), unit="ms", tz="UTC")
    n_pos_oos = sum(1 for _, _, b in rows if b["pnl_per_m"] > 0 and b["n"] >= 20)
    n_oos_elig = sum(1 for _, _, b in rows if b["n"] >= 20)
    L = []
    w = L.append
    w("# Backtest retour à la moyenne WTI/Brent sur Variational (USOILP / UKOILP)\n")
    w(f"Généré par `tools/pair_backtest.py`. Données : {len(m.ts)} minutes communes du {first:%d/%m/%Y %H:%M} "
      f"au {last:%d/%m/%Y %H:%M} UTC, {len(sids)} séances. Aucun ordre, aucun appel réseau.\n")
    w("## Hypothèses\n")
    w("- Signal : r = log(USOILP / UKOILP) en pb, sur les clôtures 1 min. Moyenne et écart-type glissants "
      "calculés séance par séance, uniquement sur les bougies déjà closes (réinitialisés à chaque ouverture : "
      "aucune fenêtre ne traverse une coupure).")
    w("- Décision à la clôture de la bougie t, exécution dans la bougie t+1 : délai de 2 s, puis 4 ordres "
      "de 2 500 $ à 1 s d'écart (WTI, Brent, WTI, Brent). Le mid de chaque tranche est interpolé "
      "linéairement entre l'ouverture et la clôture de la bougie t+1 à la seconde d'envoi : le déséquilibre "
      "entre jambes pendant l'entrée et la sortie est donc valorisé.")
    w(f"- Coût par ordre : demi-spread mesuré + glissement. Modèle : {cost.source} ; demi-spread "
      f"USOILP {fmt(cost.half(WTI, -1), 3)} pb, UKOILP {fmt(cost.half(BRENT, -1), 3)} pb (spreads.csv "
      "contient encore trop peu de mesures pour un profil horaire, médiane utilisée). Glissement de base "
      "0,5 pb par ordre, sensibilité à 0 et 1 pb.")
    w("- Sortie : retour à la cible (évaluée à chaque clôture de minute), sinon timeout à 20 min "
      "(ordres de sortie envoyés au début de la 20e minute après l'entrée), fermeture forcée 10 min avant la "
      "clôture de séance, pas d'entrée dans les 20 dernières minutes, une seule paire à la fois.")
    w("- Stop journalier : PnL réalisé de la séance + latent (au mid, à chaque clôture de minute) <= -100 $ : "
      "on ferme et plus aucune entrée jusqu'à la séance suivante. Pas de stop par paire.")
    w("- Séances déduites des trous de données : minutes communes du dimanche 22:00 au lundi 21:00, puis "
      "00:00 à 21:00 UTC du mardi au vendredi (UKOILP rouvre à 00:00 en semaine alors que USOILP rouvre à "
      "22:00). Aucune position ne traverse une coupure.")
    w("- Portage (funding) des swaps Var : les positions ne franchissent jamais la clôture quotidienne de "
      "21:00 UTC, le portage est donc nul et n'est pas modélisé.")
    w(f"- Découpage : {len(is_days)} premières séances en échantillon (IS, jusqu'au {_day_label(m, is_days[-1])}), "
      f"{len(oos_days)} suivantes hors échantillon (OOS, du {_day_label(m, oos_days[0])} au "
      f"{_day_label(m, oos_days[-1])}, dernière séance partielle). Walk-forward : 7 séances d'entraînement, "
      "3 de test, pas de 3.")
    w(f"- Critère de choix : PnL/M en échantillon, au glissement de 0,5 pb, avec au moins {min_is} trades "
      "en échantillon (2 par jour) pour écarter les configurations à trop peu de trades. Volume = somme des "
      "nominaux des 8 ordres (20 000 $ par cycle).")
    w(f"- Grille ({len(grid)} configurations) : z-score (W dans 30, 60, 120, 240 min ; z_in 1,5 / 2 / 2,5 / 3 ; "
      "z_out 0 / 0,5 / 1) et écart fixe à la moyenne (W identiques ; entrée 10 / 15 / 20 / 30 pb ; sortie "
      "0 / 5 pb), croisés avec : filtre de pente (pas d'entrée si la moyenne glissante a bougé de plus de 3 pb "
      "sur 30 min, ou coupé), filtre d'écart extrême (pas d'entrée si |z| > 4, ou coupé), pause de 0 ou 5 min "
      "après une sortie.\n")

    w("## Configuration recommandée\n")
    w(f"**{chosen.label()}** (choisie sur l'échantillon seulement).\n")
    w(_mt_table([("IS (choix)", a_is), ("OOS", a_oos), ("walk-forward, config fixe", wf_fixed),
                 ("walk-forward, re-choix par pli", wf_all), ("toute la période", a_all)]))
    w("")
    w("Sensibilité au glissement (par ordre, en plus du demi-spread) :\n")
    w("| glissement | IS PnL/M | OOS PnL/M | OOS PnL $ | OOS pire jour $ |\n|---|---|---|---|---|")
    for slip in (0.0, 0.5, 1.0):
        a, b = metrics(res[slip][chosen], is_days), metrics(res[slip][chosen], oos_days)
        w(f"| {fmt(slip, 1)} pb | {fmt(a['pnl_per_m'])} | {fmt(b['pnl_per_m'])} | {fmt(b['pnl'], 0)} | {fmt(b['worst_day'], 0)} |")
    w("")
    w("![PnL cumulé](pair_equity.png)\n")
    w("![PnL par heure](pair_pnl_by_hour.png)\n")

    w("## Top 10 choisi en échantillon (classement par PnL/M IS, colonnes OOS à côté)\n")
    w("C'est le tableau honnête : le classement n'utilise que l'échantillon. Glissement 0,5 pb. "
      "« surapprentissage » = oui si le PnL/M OOS est négatif ou inférieur à la moitié du PnL/M IS.\n")
    w(HDR)
    for p, a, b in by_is[:10]:
        w(_row(p, a, b))
    w("")
    w("## Top 10 classé sur le hors échantillon (informatif, biaisé)\n")
    w("Classer sur l'OOS revient à choisir sur le test : ces chiffres surestiment ce qu'on obtiendrait en "
      f"réel. Sur {n_oos_elig} configurations avec au moins 20 trades OOS, {n_pos_oos} sont positives en OOS.\n")
    w(HDR)
    for p, a, b in by_oos[:10]:
        w(_row(p, a, b))
    w("")
    w("## Walk-forward (7 séances d'entraînement, 3 de test)\n")
    w("| entraînement | test | config choisie sur l'entraînement | train PnL/M | test PnL/M | test PnL $ | "
      "test trades | config recommandée fixe : test PnL/M |\n|---|---|---|---|---|---|---|---|")
    for tr_d, te_d, bp, a, b, fx in wf_rows:
        w(f"| {_day_label(m, tr_d[0])} au {_day_label(m, tr_d[-1])} | {_day_label(m, te_d[0])} au "
          f"{_day_label(m, te_d[-1])} | {bp.label()} | {fmt(a['pnl_per_m'])} | {fmt(b['pnl_per_m'])} | "
          f"{fmt(b['pnl'], 0)} | {b['n']} | {fmt(fx['pnl_per_m'])} |")
    w("")
    w(f"Bilan des plis de test enchaînés ({wf_all['days']} séances) : re-choix par pli "
      f"{fmt(wf_all['pnl_per_m'])} $/M ({fmt(wf_all['pnl'], 0)} $), config recommandée fixe "
      f"{fmt(wf_fixed['pnl_per_m'])} $/M ({fmt(wf_fixed['pnl'], 0)} $). La config fixe a été choisie sur les "
      "60 premiers pour cent : sur les premiers plis de test elle est en partie en échantillon.\n")

    if sanity:
        w("## Contrôle intra-minute sur le mid Var 1 Hz du 23/09\n")
        w("| actif | minutes | écart moyen réel vs interpolation (pb) | écart-type (pb) | mouvement sur 3 s, écart-type (pb) | p95 |"
          "\n|---|---|---|---|---|---|")
        for a, v in sanity.items():
            w(f"| {a} | {v['n_min']} | {fmt(v['err_mean_bps'], 2)} | {fmt(v['err_std_bps'], 2)} | "
              f"{fmt(v['move3s_std_bps'], 2)} | {fmt(v['move3s_p95_bps'], 2)} |")
        w("\nL'interpolation n'est pas biaisée en moyenne (écart moyen proche de 0) ; son erreur est un bruit "
          "symétrique qui ne crée pas de gain systématique. Le mouvement typique pendant les 3 s d'exécution "
          "donne l'ordre de grandeur du risque de déséquilibre entre jambes.\n")
    _diagnostics_and_verdict(w, m, cost, sids, is_days, oos_days, res, rows, chosen, a_is, a_oos, wf_all,
                             wf_fixed, n_pos_oos, n_oos_elig)
    (OUT / "pair_report.md").write_text("\n".join(L) + "\n")
    (OUT / "pair_summary.json").write_text(json.dumps({
        "chosen": chosen.label(), "is": a_is, "oos": a_oos, "wf_fixed": wf_fixed, "wf_rechoice": wf_all,
        "slip": {str(s): [metrics(res[s][chosen], is_days)["pnl_per_m"], metrics(res[s][chosen], oos_days)["pnl_per_m"]]
                 for s in (0.0, 0.5, 1.0)},
        "n_pos_oos": n_pos_oos, "n_oos_elig": n_oos_elig,
        "top_is": [[p.label(), a["pnl_per_m"], b["pnl_per_m"], b["n"]] for p, a, b in by_is[:10]],
        "wf": [[bp.label(), b["pnl_per_m"], fx["pnl_per_m"]] for _, _, bp, a, b, fx in wf_rows],
        "sanity": sanity}, indent=1, default=float))


def _diagnostics_and_verdict(w, m, cost, sids, is_days, oos_days, res, rows, chosen, a_is, a_oos, wf_all,
                             wf_fixed, n_pos_oos, n_oos_elig):
    base = res[0.5]
    floor_spread = (cost.half(WTI, -1) + cost.half(BRENT, -1)) / 2 * 100     # $/M : moitié du volume par jambe
    vr = variance_ratios(m)
    gross_all = [gross_per_m(metrics(tr, sids)) for tr in base.values()]
    n_pos0 = sum(1 for tr in res[0.0].values()
                 if (b := metrics(tr, oos_days))["n"] >= 20 and b["pnl_per_m"] > 0)
    best_vol = max(rows, key=lambda r: r[1]["vol_day"])
    w("## Pourquoi : l'avantage brut est trop faible face aux coûts\n")
    w(f"- Plancher de coût sans glissement : demi-spread moyen des deux jambes, soit {fmt(floor_spread)} $/M ; "
      f"avec 0,5 pb de glissement par ordre il monte à {fmt(floor_spread + 50)} $/M, et à {fmt(floor_spread + 100)} $/M "
      "avec 1 pb.")
    w(f"- PnL/M brut (avant spread et glissement) sur toute la période, pour les {len(gross_all)} configurations : "
      f"médiane {fmt(float(np.median(gross_all)))} $/M, meilleure {fmt(max(gross_all))} $/M. La configuration "
      f"retenue fait {fmt(gross_per_m(a_is))} $/M brut en IS et {fmt(gross_per_m(a_oos))} $/M brut en OOS.")
    w(f"- Le ratio est proche d'une marche aléatoire à ces horizons : rapport de variance "
      + ", ".join(f"{fmt(v, 2)} à {k} min" for k, v in vr.items())
      + " (1 = marche aléatoire, < 1 = retour à la moyenne). Le retour à la moyenne existe mais il est "
      "faible, et la sortie forcée à 20 min coupe la partie lente.")
    w(f"- Même sans glissement (demi-spread seul), {n_pos0} configurations sur {n_oos_elig} sont positives hors "
      "échantillon avec au moins 20 trades.")
    w(f"- Pour le volume : la configuration la plus active ({best_vol[0].label()}) traite "
      f"{fmt(best_vol[1]['vol_day'] / 1000, 0)} k$ par jour en IS, à {fmt(best_vol[1]['pnl_per_m'])} $/M IS et "
      f"{fmt(best_vol[2]['pnl_per_m'])} $/M OOS : c'est en pratique le prix du volume (coût de transaction), "
      "pas un gain.\n")

    w("## PnL par séance de la configuration retenue\n")
    w("| séance (clôture) | échantillon | trades | PnL net $ | volume k$ |\n|---|---|---|---|---|")
    tr = base[chosen]
    for s_ in sids:
        mt = metrics(tr, [s_])
        w(f"| {_day_label(m, s_)} | {'IS' if s_ in is_days else 'OOS'} | {mt['n']} | {fmt(mt['pnl'], 1)} | "
          f"{fmt(mt['vol'] / 1000, 0)} |")
    w("")

    ok = a_oos["pnl_per_m"] > 0 and wf_all["pnl_per_m"] > 0
    w("## Verdict\n")
    if ok:
        w(f"La configuration retenue est positive hors échantillon ({fmt(a_oos['pnl_per_m'])} $/M) et en "
          f"walk-forward ({fmt(wf_all['pnl_per_m'])} $/M). À confirmer en papier avant tout réel.\n")
    else:
        w(f"**Non rentable hors échantillon.** La meilleure configuration choisie en échantillon "
          f"({chosen.label()}) est déjà négative en échantillon ({fmt(a_is['pnl_per_m'])} $/M), puis "
          f"{fmt(a_oos['pnl_per_m'])} $/M hors échantillon et {fmt(wf_all['pnl_per_m'])} $/M en walk-forward "
          f"avec re-choix des paramètres à chaque pli (config fixe : {fmt(wf_fixed['pnl_per_m'])} $/M). "
          f"Aucune des {n_oos_elig} configurations testées (au moins 20 trades OOS) n'est positive hors "
          f"échantillon au glissement de 0,5 pb ({n_pos_oos} positives). La stratégie telle que spécifiée "
          "(20 min max, 5 000 $ par jambe, ordres preneurs) ne doit pas être lancée en réel : elle achète du "
          "volume à un coût proche du coût de transaction pur, sans avantage mesurable.\n")
        w("Pistes si l'on veut retenter : ordres passifs (maker) pour ne pas payer le spread, horizon de "
          "sortie plus long que 20 min (le retour à la moyenne est plus net au-delà de l'heure), ou utiliser "
          "cette mécanique uniquement comme outil de volume en acceptant son coût.\n")
    w("## Risques restants\n")
    w("- Historique court : 24 séances, un seul régime de marché ; quelques dizaines de trades par "
      "configuration sélective, donc des PnL/M très bruités (un trade de 15 $ pèse 7 $/M sur 2 M$).")
    w("- Régime de tendance du 29/09 au 03/10 : le log-ratio a perdu environ 510 pb en 4 séances (de -660 à "
      "-1 185 pb), après une première baisse d'environ 240 pb du 21 au 23/09. En tendance, les paires sortent "
      "au timeout contre elles ; ce régime tombe dans la partie hors échantillon et peut revenir.")
    w(f"- Modèle de coût fragile : {cost.source} seulement, pas de profil horaire ; les spreads à l'ouverture "
      "et près de la clôture sont probablement plus larges. Le glissement réel des ordres de 2 500 $ n'est pas "
      "mesuré.")
    w("- Bougies issues de l'API du front Var (mid ou mark, pas des prix exécutables) ; le contrôle 1 Hz montre "
      "environ 2 pb d'écart-type entre le mid réel et l'interpolation dans la minute, et autant de mouvement "
      "sur les 3 s d'envoi des tranches : le déséquilibre entre jambes est du bruit, pas un biais, mais il "
      "élargit la dispersion des trades.")
    w("- Rejets, limites de débit et latence de l'API Var non modélisés (une tranche refusée laisse une jambe "
      "seule) ; le stop journalier de 100 $ n'a jamais été atteint dans l'historique.")
    w("- Portage nul uniquement parce que les positions ne franchissent jamais 21:00 UTC ; toute évolution "
      "vers des tenues plus longues devra l'intégrer.")


# ---------------------------------------------------------------- étude volume sous plafond de coût

VSLIPS = (0.0, 0.25, 0.5, 1.0)
CAP_TARGET, CAP_HARD = 70.0, 80.0
_G: dict = {}


def build_volume_grid() -> list[Params]:
    """Familles : cycle sans signal, signal léger à tenue courte, z-score et écart fixe existants.
    Tailles 2 500 $ (2 ordres par côté) et 5 000 $ (4 ordres) par jambe pour les deux premières."""
    g = []
    for size in (2500.0, 5000.0):
        for (mode, w), h, c in itertools.product((("alt", 30), ("z", 30), ("z", 60), ("z", 120)),
                                                 (1, 2, 5, 10, 20), (0, 1, 5)):
            g.append(Params("cycle", w, dir_mode=mode, hold_max=h, cooldown=c, leg_usd=size))
        for w, h, ex in itertools.product((30, 60, 120), (2, 5, 10, 20), (0.0, -1.0)):
            for zi in (0.5, 1.0, 1.5):
                g.append(Params("z", w, zi, ex, hold_max=h, leg_usd=size))
            for bi in (5.0, 7.5, 10.0):
                g.append(Params("bps", w, bi, ex, hold_max=h, leg_usd=size))
    return g + build_grid()


def family(p: Params) -> str:
    if p.method == "cycle":
        return "1. cycle sans signal"
    light = (p.method == "z" and p.entry <= 1.5) or (p.method == "bps" and p.entry <= 10)
    return "2. signal léger" if light and p.hold_max < 20 or light and p.exit < 0 else "3. z / écart fixe"


def _sim_job(args):
    p, slip = args
    q = Params(**{**asdict(p), "slip_bps": slip})
    return p, slip, simulate(_G["m"], _G["feats"][p.window], q, _G["cost"])


def run_grid(m, feats, cost, configs, slips=VSLIPS, procs: int = 0) -> dict:
    """{slip: {Params: trades}} ; en parallèle (fork) si procs > 1."""
    import multiprocessing as mp
    _G.update(m=m, feats=feats, cost=cost)
    jobs = [(p, s) for s in slips for p in configs]
    out: dict = {s: {} for s in slips}
    procs = procs or max(1, (mp.cpu_count() or 2) - 1)
    if procs > 1:
        with mp.get_context("fork").Pool(procs) as pool:
            for p, s, tr in pool.imap_unordered(_sim_job, jobs, chunksize=4):
                out[s][p] = tr
    else:
        for j in jobs:
            p, s, tr = _sim_job(j)
            out[s][p] = tr
    return out


def choose_hours(trades: list[dict], days: list[int], cap: float = CAP_TARGET, min_trades: int = 10) -> tuple:
    """Heures d'entrée dont le coût mesuré sur les jours donnés (échantillon) est <= cap $/M."""
    dayset = set(days)
    df = pd.DataFrame([t for t in trades if t["sid"] in dayset])
    if df.empty:
        return ()
    g = df.groupby("entry_hour").agg(pnl=("pnl", "sum"), vol=("vol", "sum"), n=("pnl", "size"))
    ok = g[(g["n"] >= min_trades) & (-g["pnl"] / g["vol"] * 1e6 <= cap)]
    return tuple(int(h) for h in ok.index)


def select_volume(res05: dict, days: list[int], cap: float = CAP_TARGET, min_trades: int = 30):
    """Parmi les configs à coût <= cap $/M sur ces jours (glissement 0,5 pb), la plus gros volume/jour."""
    best, best_v = None, -1.0
    for p, tr in res05.items():
        mt = metrics(tr, days)
        if mt["n"] >= min_trades and mt["cost_per_m"] <= cap and mt["vol_day"] > best_v:
            best, best_v = p, mt["vol_day"]
    return best


def pareto(points: list[tuple]) -> list[tuple]:
    """points = (cout $/M, volume/jour, objet) ; garde ceux qu'aucun autre ne bat à la fois en coût
    (plus bas) et en volume (plus haut)."""
    out, best_vol = [], -1.0
    for c, v, o in sorted(points, key=lambda x: (x[0], -x[1])):
        if v > best_vol:
            out.append((c, v, o))
            best_vol = v
    return out


def _hour_variants(base_res: dict, days: list[int], top: int = 40) -> list[Params]:
    """Variantes à filtre horaire, heures choisies sur `days` seulement, pour les configs les plus
    actives dont le coût est proche du plafond (<= 90 $/M)."""
    cands = []
    for p, tr in base_res.items():
        if p.hours:
            continue
        mt = metrics(tr, days)
        if mt["n"] >= 30 and mt["cost_per_m"] <= 90:
            cands.append((mt["vol_day"], p, tr))
    out = []
    for _, p, tr in sorted(cands, key=lambda x: -x[0])[:top]:
        hrs = choose_hours(tr, days)
        if hrs and len(hrs) < 24:
            out.append(Params(**{**asdict(p), "hours": hrs}))
    return list(dict.fromkeys(out))


def run_volume(args) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    df = load_candles()
    m = Market.from_df(df)
    cost = load_cost_model()
    sids = sorted(int(s) for s in np.unique(m.sid))
    is_days, oos_days = split_days(sids, 0.6)
    feats = {w: compute_features(m.logr_bps, m.sid, w) for w in (30, 60, 120, 240)}
    grid = build_volume_grid()
    res = run_grid(m, feats, cost, grid)
    hv = _hour_variants(res[0.5], is_days)
    if hv:
        rh = run_grid(m, feats, cost, hv)
        for s_ in VSLIPS:
            res[s_].update(rh[s_])
    allp = list(res[0.5])
    base = res[0.5]
    M = {p: (metrics(base[p], is_days), metrics(base[p], oos_days)) for p in allp}
    be = {}
    for p in allp:
        c0i, c1i = metrics(res[0.0][p], is_days)["cost_per_m"], metrics(res[1.0][p], is_days)["cost_per_m"]
        c0o, c1o = metrics(res[0.0][p], oos_days)["cost_per_m"], metrics(res[1.0][p], oos_days)["cost_per_m"]
        be[p] = {"is70": break_even_slip(c0i, c1i, CAP_TARGET), "is80": break_even_slip(c0i, c1i, CAP_HARD),
                 "oos70": break_even_slip(c0o, c1o, CAP_TARGET), "oos80": break_even_slip(c0o, c1o, CAP_HARD)}
    pick = select_volume(base, is_days)
    fallback = pick is None
    if fallback:      # rien sous 70 $/M en IS : config la moins chère parmi celles à volume réel
        pick = min((p for p in allp if M[p][0]["n"] >= 30), key=lambda p: M[p][0]["cost_per_m"])

    # walk-forward : re-choix par pli (mêmes règles, heures re-choisies sur l'entraînement du pli)
    folds = walk_forward_folds(sids, 7, 3, 3)
    base_nohour = {p: tr for p, tr in base.items() if not p.hours}
    wf_rows, wf_tr, wf_days, fixed_tr = [], [], [], []
    for tr_d, te_d in folds:
        cand = dict(base_nohour)
        hv_f = _hour_variants(base_nohour, tr_d, top=10)
        if hv_f:
            cand.update(run_grid(m, feats, cost, hv_f, slips=(0.5,))[0.5])
        bp = select_volume(cand, tr_d, min_trades=14)
        if pick.hours:          # config retenue : heures re-choisies sur l'entraînement du pli
            pb_ = Params(**{**asdict(pick), "hours": ()})
            hrs = choose_hours(base[pb_], tr_d) if pb_ in base else pick.hours
            fp = Params(**{**asdict(pick), "hours": hrs or pick.hours})
            ftr = run_grid(m, feats, cost, [fp], slips=(0.5,), procs=1)[0.5][fp]
        else:
            fp, ftr = pick, base[pick]
        fx = metrics(ftr, te_d)
        fixed_tr += [t for t in ftr if t["sid"] in set(te_d)]
        if bp is None:
            wf_rows.append((tr_d, te_d, None, None, None, fx, fp))
            wf_days += te_d
            continue
        a, b = metrics(cand[bp], tr_d), metrics(cand[bp], te_d)
        wf_rows.append((tr_d, te_d, bp, a, b, fx, fp))
        wf_tr += [t for t in cand[bp] if t["sid"] in set(te_d)]
        wf_days += te_d
    wf_all, wf_fixed = metrics(wf_tr, wf_days), metrics(fixed_tr, wf_days)

    _volume_plots(m, pick, base, M, is_days, oos_days, fixed_tr)
    _volume_report(m, cost, sids, is_days, oos_days, grid, hv, res, M, be, pick, fallback, wf_rows,
                   wf_all, wf_fixed)
    print(f"retenue : {pick.label()}  rapport : {OUT / 'volume_report.md'}")


def _volume_plots(m, pick, base, M, is_days, oos_days, fixed_tr):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(1, 2, figsize=(15, 5.5), sharey=True)
    for ax, k, title in ((axes[0], 0, "échantillon (IS)"), (axes[1], 1, "hors échantillon (OOS)")):
        pts = [(M[p][k]["cost_per_m"], M[p][k]["vol_day"] / 1000, p) for p in M if M[p][k]["n"] >= 20]
        for fam, col in (("1. cycle sans signal", "#1f6feb"), ("2. signal léger", "#e3b341"), ("3. z / écart fixe", "#8c959f")):
            xs = [(c, v) for c, v, p in pts if family(p) == fam]
            if xs:
                ax.scatter(*zip(*xs), s=9, alpha=0.5, color=col, label=fam)
        fr = pareto(pts)
        ax.plot([c for c, _, _ in fr], [v for _, v, _ in fr], color="#d1242f", lw=1.5, marker="o", ms=3,
                label="frontière de Pareto")
        c, v = M[pick][k]["cost_per_m"], M[pick][k]["vol_day"] / 1000
        ax.scatter([c], [v], s=120, marker="*", color="black", zorder=5, label="config retenue")
        for cap, ls in ((CAP_TARGET, "--"), (CAP_HARD, ":")):
            ax.axvline(cap, color="#555", ls=ls, lw=1)
        ax.set_xlim(0, 200)
        ax.set_xlabel("coût $/M (glissement 0,5 pb par ordre)")
        ax.set_title(f"Volume par jour vs coût, {title}")
        ax.grid(alpha=0.3)
    axes[0].set_ylabel("volume par jour (k$)")
    axes[0].legend(fontsize=8, loc="upper left")
    fig.tight_layout()
    fig.savefig(OUT / "volume_pareto.png", dpi=110)
    plt.close(fig)

    t = pd.DataFrame(base[pick]).sort_values("exit_ts")
    fig, ax = plt.subplots(2, 1, figsize=(12, 7), sharex=True)
    tm = pd.to_datetime(t["exit_ts"], unit="ms", utc=True)
    ax[0].plot(tm, t["pnl"].cumsum(), color="#1f6feb", lw=1.3, label=pick.label())
    ax[0].set_ylabel("PnL cumulé $")
    ax[1].plot(tm, t["vol"].cumsum() / 1e6, color="#1f883d", lw=1.3)
    ax[1].set_ylabel("volume cumulé M$")
    split = pd.Timestamp(int(m.ts[np.argmax(m.sid == oos_days[0])]), unit="ms", tz="UTC")
    for a in ax:
        a.axvline(split, color="#555", ls="--", lw=1)
        a.grid(alpha=0.3)
    ax[0].legend(loc="lower left", fontsize=9)
    ax[0].set_title("Config retenue : PnL et volume cumulés (glissement 0,5 pb), trait = début du hors échantillon")
    ax[1].xaxis.set_major_formatter(mdates.DateFormatter("%d/%m"))
    fig.tight_layout()
    fig.savefig(OUT / "volume_equity.png", dpi=110)
    plt.close(fig)


def _fs(x: float) -> str:
    if x == float("inf"):
        return "illimité"
    return "< 0 (déjà au-dessus)" if x < 0 else f"{fmt(x, 2)} pb"


def _volume_report(m, cost, sids, is_days, oos_days, grid, hv, res, M, be, pick, fallback, wf_rows, wf_all, wf_fixed):
    base = res[0.5]
    a_is, a_oos = M[pick]
    first = pd.Timestamp(int(m.ts[0]), unit="ms", tz="UTC")
    last = pd.Timestamp(int(m.ts[-1]), unit="ms", tz="UTC")
    L = []
    w = L.append
    w("# Volume maximal sous plafond de coût, paire WTI/Brent sur Variational\n")
    w(f"Généré par `tools/pair_backtest.py --volume`. {len(m.ts)} minutes communes du {first:%d/%m/%Y %H:%M} au "
      f"{last:%d/%m/%Y %H:%M} UTC, {len(sids)} séances. Aucun ordre, aucun appel réseau. Objectif : le plus "
      f"de volume par jour avec un coût hors échantillon <= {CAP_TARGET:g} $/M (plafond dur {CAP_HARD:g} $/M).\n")
    w("## Hypothèses\n")
    w("- Coût = -PnL net / volume, en $ par million traité (volume = somme des nominaux de tous les ordres).")
    w(f"- Spread : {cost.source}, demi-spread USOILP {fmt(cost.half(WTI, -1), 3)} pb, UKOILP "
      f"{fmt(cost.half(BRENT, -1), 3)} pb, identique à 2 500 et 5 000 $ dans les mesures (pas de profil horaire "
      "possible). Glissement testé : 0 / 0,25 / 0,5 / 1 pb par ordre. Le coût croît de 100 $/M par pb de "
      "glissement : à 0 pb, un aller-retour sans avantage coûte environ le demi-spread moyen, "
      f"{fmt((cost.half(WTI, -1) + cost.half(BRENT, -1)) * 50)} $/M.")
    w("- Exécution : décision à la clôture de la bougie t (données <= t seulement), ordres alternés WTI / Brent "
      "dans la bougie suivante, 2 s de délai puis 1 ordre toutes les 1,5 s ; 4 ordres par côté à 5 000 $ par "
      "jambe, 2 ordres par côté à 2 500 $. Mid de chaque ordre interpolé dans la minute (déséquilibre entre "
      "jambes valorisé).")
    w("- Famille cycle : ouverture, tenue H min, fermeture, réouverture après C min ; avec C = 0 la "
      "réouverture se fait dans la même minute, juste après les ordres de sortie. Direction alternée, ou "
      "contre le signe du z courant (W = fenêtre du z).")
    w("- Limites : une paire à la fois, plafond de 600 ordres sur toute heure glissante (entrée refusée sinon), "
      "1 ordre / 1,5 s dans les rafales, stop journalier 100 $ (réalisé + latent), sortie forcée 10 min avant "
      "la clôture, pas d'entrée dans les 20 dernières minutes, rien à travers les coupures. Portage nul (aucune "
      "position ne franchit 21:00 UTC).")
    w("- Garantie : 5 000 $ par jambe à levier 5 = environ 2 000 $ (1 000 $ à 2 500 $ par jambe).")
    w(f"- Découpage : {len(is_days)} séances IS (jusqu'au {_day_label(m, is_days[-1])}), {len(oos_days)} séances "
      f"OOS (du {_day_label(m, oos_days[0])} au {_day_label(m, oos_days[-1])}, dernière partielle). Walk-forward "
      "7 séances d'entraînement, 3 de test, pas de 3.")
    w(f"- Sélection : en IS, configs avec au moins 30 trades et coût <= {CAP_TARGET:g} $/M à 0,5 pb, puis le plus "
      "gros volume par jour. Filtre horaire : heures dont le coût IS est <= 70 $/M (au moins 10 trades), "
      "choisies sur l'IS seulement (et sur l'entraînement de chaque pli en walk-forward).")
    nfam = pd.Series([family(p) for p in grid]).value_counts().sort_index()
    w(f"- Grille : {len(grid)} configurations (" + ", ".join(f"{k} : {v}" for k, v in nfam.items())
      + f") + {len(hv)} variantes à filtre horaire. Cycle : direction alternée ou z (W 30, 60, 120), H dans "
      "1, 2, 5, 10, 20 min, C dans 0, 1, 5 min. Signal léger : z_in 0,5 / 1 / 1,5 ou écart 5 / 7,5 / 10 pb, "
      "W 30 / 60 / 120, tenue max 2 / 5 / 10 / 20 min, sortie au retour à la moyenne ou seulement au timeout. "
      "Plus les 640 configs z-score / écart fixe du premier rapport (5 000 $).\n")

    w("## Config retenue\n")
    if fallback:
        w(f"**Aucune configuration n'atteint {CAP_TARGET:g} $/M en échantillon à 0,5 pb de glissement.** Repli "
          "affiché : la moins chère en IS.\n")
    w(f"**{pick.label()}** ({family(pick)[3:]}).\n")
    cols = [("IS", a_is), ("OOS", a_oos), ("walk-forward, config fixe", wf_fixed),
            ("walk-forward, re-choix par pli", wf_all)]
    keys = [("coût $/M", "cost_per_m", 1), ("volume/jour k$", "vol_day", None), ("PnL net $", "pnl", 0),
            ("trades", "n", 0), ("séances", "days", 0), ("cycles/jour", "cycles_day", 1),
            ("ordres/jour", "orders_day", 0), ("pic d'ordres sur une heure", "peak_orders_h", 0),
            ("pire jour $", "worst_day", 1), ("séances arrêtées par le stop de 100 $", "stop_days", 0),
            ("drawdown max $", "max_dd", 1), ("pire trade $", "worst_trade", 2),
            ("taux de gain", "hit", "%"), ("sorties timeout", "pct_timeout", "%"), ("sorties cible", "pct_cible", "%"),
            ("sorties stop jour", "pct_stop", "%"), ("sorties fin de séance", "pct_seance", "%")]
    w("| | " + " | ".join(c for c, _ in cols) + " |\n|---|" + "---|" * len(cols))
    for lab, k, d in keys:
        vals = []
        for _, mt in cols:
            if d == "%":
                vals.append(pct(mt[k]))
            elif d is None:
                vals.append(fmt(mt[k] / 1000, 0))
            elif k in ("n", "days", "peak_orders_h", "stop_days"):
                vals.append(str(int(mt[k])))
            else:
                vals.append(fmt(mt[k], d))
        w(f"| {lab} | " + " | ".join(vals) + " |")
    w("")
    w("Sensibilité au glissement (par ordre, en plus du demi-spread) :\n")
    w("| glissement | IS coût $/M | IS volume/jour k$ | OOS coût $/M | OOS volume/jour k$ | OOS pire jour $ | OOS sorties stop jour |"
      "\n|---|---|---|---|---|---|---|")
    for sl in VSLIPS:
        a, b = metrics(res[sl][pick], is_days), metrics(res[sl][pick], oos_days)
        w(f"| {fmt(sl, 2)} pb | {fmt(a['cost_per_m'])} | {fmt(a['vol_day'] / 1000, 0)} | {fmt(b['cost_per_m'])} | "
          f"{fmt(b['vol_day'] / 1000, 0)} | {fmt(b['worst_day'], 0)} | {pct(b['pct_stop'])} |")
    bb = be[pick]
    w(f"\nGlissement de bascule : coût à {CAP_TARGET:g} $/M au-delà de {_fs(bb['is70'])} (IS) / {_fs(bb['oos70'])} (OOS), "
      f"à {CAP_HARD:g} $/M au-delà de {_fs(bb['is80'])} (IS) / {_fs(bb['oos80'])} (OOS). C'est le glissement réel "
      "moyen par ordre à ne pas dépasser, à mesurer en petit test réel.\n")
    w("![PnL et volume cumulés](volume_equity.png)\n")

    w("## Walk-forward\n")
    w("| entraînement | test | config re-choisie | train coût $/M | test coût $/M | test vol/jour k$ | "
      "config retenue fixe : test coût $/M | fixe : test vol/jour k$ | fixe sous 70 / 80 |\n|---|---|---|---|---|---|---|---|---|")
    n70 = n80 = 0
    for tr_d, te_d, bp, a, b, fx, fp in wf_rows:
        ok70, ok80 = fx["cost_per_m"] <= CAP_TARGET, fx["cost_per_m"] <= CAP_HARD
        n70 += ok70
        n80 += ok80
        lab = bp.label() if bp else "aucune sous le plafond"
        w(f"| {_day_label(m, tr_d[0])} au {_day_label(m, tr_d[-1])} | {_day_label(m, te_d[0])} au {_day_label(m, te_d[-1])} | "
          f"{lab} | {fmt(a['cost_per_m']) if a else '-'} | {fmt(b['cost_per_m']) if b else '-'} | "
          f"{fmt(b['vol_day'] / 1000, 0) if b else '-'} | {fmt(fx['cost_per_m'])} | {fmt(fx['vol_day'] / 1000, 0)} | "
          f"{'oui' if ok70 else 'non'} / {'oui' if ok80 else 'non'} |")
    w(f"\nStabilité : la config retenue (fixe) est sous {CAP_TARGET:g} $/M sur {n70} plis sur {len(wf_rows)}, et sous "
      f"{CAP_HARD:g} $/M sur {n80} plis sur {len(wf_rows)}. Les premiers plis de test recouvrent l'échantillon de choix.\n")

    w("## Frontière de Pareto (volume par jour vs coût, glissement 0,5 pb)\n")
    w("![Pareto](volume_pareto.png)\n")
    w("Frontière calculée en IS (configs avec au moins 30 trades), avec le résultat OOS de chaque point :\n")
    w("| config | famille | IS coût $/M | IS vol/jour k$ | OOS coût $/M | OOS vol/jour k$ | OOS pire jour $ | OOS drawdown $ | ordres/jour | bascule 70 $/M (IS / OOS) |"
      "\n|---|---|---|---|---|---|---|---|---|---|")
    fr = pareto([(M[p][0]["cost_per_m"], M[p][0]["vol_day"], p) for p in M if M[p][0]["n"] >= 30])
    for c, v, p in fr:
        if c > 150:
            break
        a, b = M[p]
        w(f"| {p.label()} | {family(p)[3:]} | {fmt(c)} | {fmt(v / 1000, 0)} | {fmt(b['cost_per_m'])} | "
          f"{fmt(b['vol_day'] / 1000, 0)} | {fmt(b['worst_day'], 0)} | {fmt(b['max_dd'], 0)} | {fmt(b['orders_day'], 0)} | "
          f"{_fs(be[p]['is70'])} / {_fs(be[p]['oos70'])} |")
    w("\nFrontière OOS (descriptive : choisir dessus serait biaisé) :\n")
    w("| config | OOS coût $/M | OOS vol/jour k$ | IS coût $/M |\n|---|---|---|---|")
    for c, v, p in pareto([(M[p][1]["cost_per_m"], M[p][1]["vol_day"], p) for p in M if M[p][1]["n"] >= 20]):
        if c > 150:
            break
        w(f"| {p.label()} | {fmt(c)} | {fmt(v / 1000, 0)} | {fmt(M[p][0]['cost_per_m'])} |")
    w("")
    w("## Meilleure config par famille (choisie en IS, même règle)\n")
    w("| famille | config | IS coût $/M | IS vol/jour k$ | OOS coût $/M | OOS vol/jour k$ | OOS pire jour $ | bascule 70 $/M OOS |"
      "\n|---|---|---|---|---|---|---|---|")
    for fam in ("1. cycle sans signal", "2. signal léger", "3. z / écart fixe"):
        sub = {p: base[p] for p in base if family(p) == fam}
        bp = select_volume(sub, is_days) or min((p for p in sub if M[p][0]["n"] >= 30),
                                                key=lambda p: M[p][0]["cost_per_m"], default=None)
        if bp is None:
            continue
        a, b = M[bp]
        w(f"| {fam[3:]} | {bp.label()} | {fmt(a['cost_per_m'])} | {fmt(a['vol_day'] / 1000, 0)} | "
          f"{fmt(b['cost_per_m'])} | {fmt(b['vol_day'] / 1000, 0)} | {fmt(b['worst_day'], 0)} | {_fs(be[bp]['oos70'])} |")
    w("")
    under = [p for p in M if M[p][0]["n"] >= 30 and M[p][0]["cost_per_m"] <= CAP_TARGET]
    w(f"## Top 15 en volume parmi les configs sous {CAP_TARGET:g} $/M en IS ({len(under)} configs)\n")
    w("| config | IS coût $/M | IS vol/jour k$ | OOS coût $/M | OOS vol/jour k$ | OOS sous 70 / 80 | OOS pire jour $ |"
      "\n|---|---|---|---|---|---|---|")
    for p in sorted(under, key=lambda p: -M[p][0]["vol_day"])[:15]:
        a, b = M[p]
        w(f"| {p.label()} | {fmt(a['cost_per_m'])} | {fmt(a['vol_day'] / 1000, 0)} | {fmt(b['cost_per_m'])} | "
          f"{fmt(b['vol_day'] / 1000, 0)} | {'oui' if b['cost_per_m'] <= CAP_TARGET else 'non'} / "
          f"{'oui' if b['cost_per_m'] <= CAP_HARD else 'non'} | {fmt(b['worst_day'], 0)} |")
    n_ok = sum(1 for p in under if M[p][1]["cost_per_m"] <= CAP_TARGET)
    w(f"\nParmi ces {len(under)} configs sous {CAP_TARGET:g} $/M en IS, {n_ok} le restent en OOS.\n")
    _volume_verdict(w, M, be, pick, fallback, res, oos_days, wf_all, wf_fixed, n70, len(wf_rows))
    (OUT / "volume_report.md").write_text("\n".join(L) + "\n")
    (OUT / "volume_summary.json").write_text(json.dumps({
        "pick": pick.label(), "fallback": fallback, "is": a_is, "oos": a_oos, "wf_fixed": wf_fixed,
        "wf_rechoice": wf_all, "break_even": be[pick], "n_under_is": len(under), "n_under_both": n_ok,
        "slip": {str(sl): [metrics(res[sl][pick], is_days)["cost_per_m"], metrics(res[sl][pick], oos_days)["cost_per_m"],
                           metrics(res[sl][pick], oos_days)["vol_day"]] for sl in VSLIPS},
        "wf": [[bp.label() if bp else None, b["cost_per_m"] if b else None, fx["cost_per_m"], fx["vol_day"]]
               for _, _, bp, a, b, fx, fp in wf_rows]}, indent=1, default=float))


def _volume_verdict(w, M, be, pick, fallback, res, oos_days, wf_all, wf_fixed, n70, nfolds):
    a, b = M[pick]
    b25 = metrics(res[0.25][pick], oos_days)
    w("## Le stop journalier fixe le volume\n")
    w(f"Aucune config n'a d'avantage brut qui couvre le spread et le glissement : chaque config active perd à peu "
      f"près son coût de transaction, et le stop de 100 $ par jour arrête la plupart des séances "
      f"({a['stop_days']} séances sur {a['days']} en IS, {b['stop_days']} sur {b['days']} en OOS pour la config "
      f"retenue). Le volume par jour vaut donc en pratique 100 $ divisés par le coût par million : environ "
      f"1,4 M$ à 70 $/M, 1,25 M$ à 80 $/M. Baisser le coût de 10 $/M rapporte environ 200 k$ de volume par "
      f"jour ; relever le stop augmente le volume dans la même proportion (et la perte aussi).\n")
    w("## Verdict et recommandation\n")
    ok70, ok80 = b["cost_per_m"] <= CAP_TARGET, b["cost_per_m"] <= CAP_HARD
    w(f"- Config recommandée : **{pick.label()}**. Dès que |z| >= 0,5 (fenêtre 60 min), on se place contre "
      f"l'écart du ratio à sa moyenne, sans sortie à la cible, et on referme au bout de 10 min (ou au stop)."
      if pick.label().startswith("z W60 in0.5z sans cible H10") else f"- Config recommandée : **{pick.label()}**.")
    w(f"- Attendu à 0,5 pb de glissement : {fmt(b['vol_day'] / 1e6, 2)} M$ de volume par jour en OOS "
      f"({fmt(a['vol_day'] / 1e6, 2)} M$ en IS), coût {fmt(b['cost_per_m'])} $/M en OOS, "
      f"{fmt(wf_fixed['cost_per_m'])} $/M en walk-forward avec la config fixe et {fmt(wf_all['cost_per_m'])} $/M avec "
      f"re-choix par pli, pire jour {fmt(min(a['worst_day'], b['worst_day']), 0)} $, environ "
      f"{fmt(b['orders_day'], 0)} ordres par jour (pic {b['peak_orders_h']} par heure, loin des 600), garantie "
      "d'environ 2 000 $.")
    w(f"- Plafond : {'tenu' if ok70 else 'non tenu'} à {CAP_TARGET:g} $/M en OOS, {'tenu' if ok80 else 'non tenu'} à "
      f"{CAP_HARD:g} $/M ; sous {CAP_TARGET:g} $/M sur {n70} plis de walk-forward sur {nfolds}. À 0,25 pb de "
      f"glissement, l'OOS tombe à {fmt(b25['cost_per_m'])} $/M pour {fmt(b25['vol_day'] / 1e6, 2)} M$ par jour.")
    w(f"- Glissement de bascule : {_fs(be[pick]['oos70'])} par ordre pour {CAP_TARGET:g} $/M et "
      f"{_fs(be[pick]['oos80'])} pour {CAP_HARD:g} $/M (OOS). Le petit test réel doit mesurer le glissement moyen "
      "par ordre (prix obtenu vs mid au moment de l'envoi) : en dessous d'environ 0,4 pb la cible de 70 $/M est "
      "tenue, au-dessus de 0,55 pb même 80 $/M est dépassé.")
    w("- Honnêtement : c'est un achat de volume, pas une stratégie gagnante. Le signal léger (contre le z) ne "
      "fait gagner que quelques $/M par rapport au cycle aveugle ; l'écart entre les configs du haut du "
      "classement (65 à 80 $/M en OOS) est du même ordre que le bruit d'un échantillon de 10 séances.\n")
    w("## Risques\n")
    w("- Glissement réel inconnu : c'est le premier facteur (100 $/M par pb). Les 0,5 pb de base ne sont pas "
      "mesurés ; le spread lui-même ne repose que sur quelques dizaines de cotations d'une seule matinée.")
    w("- Le stop de 100 $ est atteint presque chaque jour : la perte attendue est d'environ 100 $ par jour de "
      "séance, soit environ 2 000 $ par mois pour 25 à 30 M$ de volume.")
    w("- Historique court (24 séances) et régime de tendance du 29/09 au 03/10 : les plis de test qui le "
      "contiennent dépassent 80 $/M (environ 90 à 98 $/M).")
    w("- Rejets d'ordres, latence, limites de débit réelles de Var non modélisés ; une tranche refusée laisse une "
      "jambe seule. Les bougies du front sont des mid, pas des prix exécutables (environ 2 pb de bruit sur 3 s).")
    w("- Le volume se concentre en début de séance (avant que le stop ne coupe) : si les spreads y sont plus "
      "larges que la médiane mesurée en matinée, le coût réel sera plus élevé.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sanity-only", action="store_true")
    ap.add_argument("--volume", action="store_true", help="étude volume max sous plafond de coût")
    args = ap.parse_args()
    if args.volume:
        run_volume(args)
        return
    if args.sanity_only:
        print(json.dumps(sanity_1hz(load_candles()), indent=1))
        return
    run(args)


if __name__ == "__main__":
    main()
