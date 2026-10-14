"""Comparaison offline sur CSV ts,wti,brent ; aucune connexion ni ordre.

Exécution au point suivant la décision. Les coûts doivent être fournis :
--half-spread-bps et --slip-bps, appliqués à chaque jambe et chaque ordre.
Ne modélise pas les garde-fous, la marge, le funding, les refus RFQ ni les
remboursements de pertes. Comparaison du signal/coût, pas validation live.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from arb.config import PairCfg, load_pair_config
from arb.pair_signal import RatioSignal, TrendSignal


def replay(rows, cfg: PairCfg, half_spread_bps, slip_bps, mode):
    if not all(math.isfinite(v) and v >= 0 for v in (half_spread_bps, slip_bps)):
        raise ValueError("Les coûts doivent être positifs ou nuls")
    if mode not in ("mean_reversion", "trend_hold"):
        raise ValueError("Mode invalide")
    ratio = RatioSignal(cfg.z_window_min)
    trend = TrendSignal(cfg.trend_window_min, cfg.trend_confirm_min,
                        cfg.trend_min_move_bps, cfg.trend_min_r2)
    qty = [0.0, 0.0]
    cash = volume = costs = 0.0
    side = None
    previous = "A"
    next_check = 0.0
    pending = None
    executions = 0
    equity_peak = drawdown = 0.0
    unit_cost = (half_spread_bps + slip_bps) / 1e4

    def trade(deltas, prices):
        nonlocal cash, volume, costs, executions
        for leg, delta in enumerate(deltas):
            if abs(delta) <= 1e-12:
                continue
            price = prices[leg] * (1 + math.copysign(unit_cost, delta))
            cash -= delta * price
            volume += abs(delta) * price
            costs += abs(delta) * prices[leg] * unit_cost
            qty[leg] += delta
            executions += math.ceil(abs(delta) * prices[leg] / cfg.tranche_usd - 1e-9)

    for row in rows:
        ts, wti, brent = row
        prices = [wti, brent]
        if pending is not None:
            if mode == "mean_reversion" and side == pending:
                trade([-q for q in qty], prices)
            direction = 1 if pending == "B" else -1
            target = [direction * cfg.leg_usd / wti, -direction * cfg.leg_usd / brent]
            trade([target[i] - qty[i] for i in (0, 1)], prices)
            side = previous = pending
            pending = None
        ratio.add(ts, wti, brent)
        trend.add(ts, wti, brent)
        equity = cash + sum(qty[i] * prices[i] for i in (0, 1))
        equity_peak = max(equity_peak, equity)
        drawdown = max(drawdown, equity_peak - equity)
        if ts < next_check:
            continue
        next_check = ts + cfg.cycle_min * 60
        if mode == "mean_reversion":
            pending = ratio.side(previous)
        else:
            decision = trend.status()
            desired = decision["side"]
            initial = not decision["ready"] and cfg.warmup_entry_side is not None
            if initial:
                desired = side or cfg.warmup_entry_side
            projected = abs(decision["slope_bps_min"] or 0) * cfg.cycle_min
            # Deux spreads complets, exprimés en pb d'une seule jambe.
            required = (4 * half_spread_bps + cfg.trend_slip_reserve_bps) * cfg.trend_cost_multiplier
            affordable = ((4 * half_spread_bps + cfg.trend_slip_reserve_bps) * 25 <= cfg.cost_alert_per_m
                          if initial else projected > required)
            if desired is not None and desired != side and affordable:
                pending = desired
    if not rows:
        raise ValueError("Historique vide")
    trade([-q for q in qty], [rows[-1][1], rows[-1][2]])
    drawdown = max(drawdown, equity_peak - cash)
    return {"mode": mode, "pnl_usd": cash, "volume_usd": volume,
            "cost_per_m": -cash / volume * 1e6 if volume else None,
            "transaction_cost_usd": costs, "estimated_orders": executions,
            "max_drawdown_usd": drawdown}


def read_rows(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as f:
        rows = [(float(r["ts"]), float(r["wti"]), float(r["brent"])) for r in csv.DictReader(f)]
    if any(not all(math.isfinite(v) for v in r) or min(r[1:]) <= 0 for r in rows):
        raise ValueError("Prix/horodatage invalide")
    if any(b[0] <= a[0] or b[0] - a[0] > 120 for a, b in zip(rows, rows[1:])):
        raise ValueError("Historique non chronologique ou discontinu : séparer les séances")
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv")
    parser.add_argument("--half-spread-bps", type=float, required=True)
    parser.add_argument("--slip-bps", type=float, required=True)
    args = parser.parse_args()
    cfg = load_pair_config(ROOT / "config.toml")
    rows = read_rows(args.csv)
    print(json.dumps({"simulation_only": True,
                      "comparison": [replay(rows, cfg, args.half_spread_bps, args.slip_bps, mode)
                                     for mode in ("mean_reversion", "trend_hold")]}, indent=2))


if __name__ == "__main__":
    main()
