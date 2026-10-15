"""Offline signal/cost comparison; no connection or order is sent.

Execution CSV: ts,wti,brent (observation time in Unix seconds, positive mids).
Optional --signal-csv: independent 1m candle closes with ts = candle OPEN.
These closes become available only at ts + 60; execution always uses the
execution CSV at the observation AFTER the decision. Without --signal-csv,
the signal uses execution mids as a proxy, never verified TradingView data.

Every planned tranche pays the supplied half-spread and slippage. Tranches
are ordered as in the live planner but share one execution snapshot: delays,
RFQ failures, sessions/risk guards, funding, margin and loss refunds are NOT
modelled. This is research, not a live-performance validation.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import deque
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from arb.config import PairCfg, load_pair_config
from arb.pair_exec import plan_cycle, target_usd
from arb.pair_signal import RatioSignal, TrendSignal, select_volume_side


def _validated_rows(rows):
    rows = [tuple(float(v) for v in r) for r in rows]
    if not rows:
        raise ValueError("Historique vide")
    if any(len(r) != 3 or not all(math.isfinite(v) for v in r) or min(r[1:]) <= 0 for r in rows):
        raise ValueError("Prix/horodatage invalide")
    if any(b[0] <= a[0] or b[0] - a[0] > 120 for a, b in zip(rows, rows[1:])):
        raise ValueError("Historique non chronologique ou discontinu : séparer les séances")
    return rows


def _legacy_volume_side(decision, current, previous):
    """Old volume fallback: keep held/previous direction before consulting slope."""
    return (decision["side"] or current or previous
            or ("A" if (decision["slope_bps_min"] or 0) < 0 else "B"))


def replay(rows, cfg: PairCfg, half_spread_bps, slip_bps, mode, *, signal_rows=None,
           fixed_cadence=False, include_events=False, direction_policy="adaptive", not_before_ts=None):
    """Replay one continuous session; optional fixed cadence isolates direction.

    fixed_cadence uses a fixed DECISION clock at the mandatory volume cadence.
    Every decision still executes at the next observation. With uniformly
    sampled prices this preserves the same execution cadence in both policies.
    Full replay instead uses the live last-execution clock and permits early
    flips. not_before_ts permits a common warmup for a fair comparison.
    """
    if not all(math.isfinite(v) and 0 <= v < 1e4 for v in (half_spread_bps, slip_bps)):
        raise ValueError("Les coûts doivent être positifs ou nuls et inférieurs à 10000 pb")
    if half_spread_bps + slip_bps >= 1e4:
        raise ValueError("Coût total invalide")
    if mode not in ("mean_reversion", "trend_hold") or direction_policy not in ("legacy", "adaptive"):
        raise ValueError("Mode/politique invalide")
    if not all(math.isfinite(v) and v > 0 for v in (cfg.leg_usd, cfg.tranche_usd, cfg.cycle_min)):
        raise ValueError("Tailles/cadence invalides")
    if fixed_cadence and not (cfg.volume_cycle_required and cfg.volume_cycle_min > 0):
        raise ValueError("La comparaison à cadence fixe exige le volume obligatoire")
    rows = _validated_rows(rows)
    independent = signal_rows is not None
    signals = _validated_rows(signal_rows) if independent else None
    ratio = RatioSignal(cfg.z_window_min)
    trend = TrendSignal(cfg.trend_window_min, cfg.trend_confirm_min,
                        cfg.trend_min_move_bps, cfg.trend_min_r2,
                        context_min=cfg.trend_context_min)
    closed = deque(maxlen=max(cfg.trend_window_min, cfg.trend_context_min) + 2)
    signal_index = 0
    qty = [0.0, 0.0]
    cash = volume = costs = 0.0
    side = previous = None
    next_check = 0.0
    pending = None
    executions = cycles = flips = recycles = 0
    last_execution_ts = None
    last_decision_ts = None
    latencies = []
    equity_peak = drawdown = 0.0
    unit_cost = (half_spread_bps + slip_bps) / 1e4
    events = []
    legs = ("wti", "brent")

    def volume_side(decision):
        if direction_policy == "legacy":
            return _legacy_volume_side(decision, side, previous)
        return select_volume_side(decision, current=side, previous=previous)

    def execute(desired, recycle, prices, ts, decision_ts):
        nonlocal cash, volume, costs, executions
        # Match _plan_start: drift must not create a spurious extra tranche.
        # Reductions close held QUANTITY, rather than a newly sized dollar amount.
        current = {leg: math.copysign(max(1, round(abs(qty[i]) * prices[i] / cfg.tranche_usd))
                                     * cfg.tranche_usd, qty[i]) if qty[i] else 0.0
                   for i, leg in enumerate(legs)}
        target = target_usd(desired, cfg.leg_usd) if desired else dict.fromkeys(legs, 0.0)
        steps = plan_cycle(current, target, cfg.tranche_usd, reopen=recycle)
        close_qty = {}
        for i, leg in enumerate(legs):
            indices = [n for n, step in enumerate(steps) if step.leg == leg and step.reduce_only]
            used = 0.0
            for n, step_index in enumerate(indices):
                amount = (round(abs(qty[i]) - used, 9) if n == len(indices) - 1
                          else round(abs(qty[i]) / len(indices), 3))
                close_qty[step_index] = amount
                used += amount
        orders_before, volume_before = executions, volume
        for n, step in enumerate(steps):
            i = legs.index(step.leg)
            amount = close_qty[n] if step.reduce_only else round(step.usd / prices[i], 3)
            if amount <= 0:
                continue
            delta = amount if step.side == "buy" else -amount
            price = prices[i] * (1 + math.copysign(unit_cost, delta))
            cash -= delta * price
            volume += abs(delta) * price
            costs += abs(delta) * prices[i] * unit_cost
            qty[i] = round(qty[i] + delta, 9)
            executions += 1
        if include_events and executions > orders_before:
            events.append({"decision_ts": decision_ts, "execution_ts": ts, "side": desired,
                           "recycle": recycle, "orders": executions - orders_before,
                           "volume_usd": volume - volume_before})

    for ts, wti, brent in rows:
        prices = [wti, brent]
        if pending is not None:
            desired, recycle, decision_ts = pending
            execute(desired, recycle, prices, ts, decision_ts)
            flips += int(side is not None and desired != side)
            recycles += int(recycle and side == desired)
            cycles += 1
            side = previous = desired
            last_execution_ts = ts
            last_decision_ts = decision_ts
            latencies.append(ts - decision_ts)
            pending = None
        ratio.add(ts, wti, brent)
        signal_fresh = True
        if independent:
            while signal_index < len(signals) and signals[signal_index][0] + 60 <= ts:
                closed.append(signals[signal_index])
                signal_index += 1
            signal_fresh = bool(closed) and ts - (closed[-1][0] + 60) <= cfg.tv_stale_s
            trend.replace_closed(list(closed), ts)
        else:
            trend.add(ts, wti, brent)
        equity = cash + sum(qty[i] * prices[i] for i in (0, 1))
        equity_peak = max(equity_peak, equity)
        drawdown = max(drawdown, equity_peak - equity)
        if not fixed_cadence and last_execution_ts is not None:
            next_check = max(next_check, last_execution_ts + cfg.cycle_min * 60)
        if ts < next_check or (not_before_ts is not None and ts < not_before_ts):
            continue
        next_check = ts if fixed_cadence else ts + cfg.cycle_min * 60
        decision = trend.status()
        cadence_anchor = last_decision_ts if fixed_cadence else last_execution_ts
        volume_due = (side is not None and cfg.volume_cycle_min > 0 and cadence_anchor is not None
                      and ts >= cadence_anchor + cfg.volume_cycle_min * 60)
        required = cfg.volume_cycle_required and cfg.volume_cycle_min > 0
        if fixed_cadence:
            if not signal_fresh or not decision["ready"] or (side is not None and not volume_due):
                continue
            desired = ratio.side(previous or "A") if mode == "mean_reversion" else volume_side(decision)
            pending = desired, desired == side, ts
            next_check = ts + cfg.volume_cycle_min * 60
        elif mode == "mean_reversion":
            desired = ratio.side(previous or "A")
            pending = desired, desired == side, ts
        else:
            # Manual/warmup entry overrides are outside this research model.
            if not signal_fresh or not decision["ready"]:
                continue
            desired = decision["side"]
            volume_action = required and (volume_due or side is None)
            if volume_action or volume_due:
                desired = volume_side(decision)
            if desired is None:
                continue
            recycle = volume_due and desired == side
            if desired == side and not recycle:
                continue
            projected = abs(decision["slope_bps_min"] or 0) * cfg.trend_projection_min
            quoted_cost = 4 * half_spread_bps + cfg.trend_slip_reserve_bps
            affordable = (quoted_cost * 25 <= cfg.cost_alert_per_m if volume_due
                          else projected > quoted_cost * cfg.trend_cost_multiplier)
            if volume_action or affordable:
                pending = desired, recycle, ts
        if not fixed_cadence and last_execution_ts is not None:
            next_check = max(next_check, last_execution_ts + cfg.cycle_min * 60)
    execute(None, True, [rows[-1][1], rows[-1][2]], rows[-1][0], None)
    drawdown = max(drawdown, equity_peak - cash)
    result = {"mode": mode, "direction_policy": direction_policy, "pnl_usd": cash, "volume_usd": volume,
              "cost_per_m": -cash / volume * 1e6 if volume else None,
              "transaction_cost_usd": costs, "estimated_orders": executions,
              "cycles": cycles, "flips": flips, "recycles": recycles,
              "max_drawdown_usd": drawdown, "independent_signal_prices": independent,
              "fixed_cadence": fixed_cadence,
              "cadence_clock": "fixed_decision_clock" if fixed_cadence else "last_execution_clock",
              "decision_to_execution_s_min": min(latencies) if latencies else None,
              "decision_to_execution_s_max": max(latencies) if latencies else None,
              "execution_model": "next_observation_all_tranches_at_one_price_snapshot",
              "signal_price_model": "independent_closed_candles" if independent else "execution_mid_proxy"}
    if include_events:
        result["events"] = events
    return result


def compare_fixed_cadence(rows, cfg, half_spread_bps, slip_bps, *, signal_rows=None,
                          baseline_cfg=None, include_events=False):
    """Legacy vs adaptive at common warmup/cadence/sizes/costs; no early flips.

    The baseline defaults to the same config with macro context disabled.
    Equal counts are reported, never assumed, and volumes remain explicit.
    """
    rows = _validated_rows(rows)
    signals = _validated_rows(signal_rows) if signal_rows is not None else None
    baseline_cfg = baseline_cfg or replace(cfg, trend_context_min=0)
    if any(getattr(baseline_cfg, key) != getattr(cfg, key) for key in
           ("leg_usd", "tranche_usd", "cycle_min", "volume_cycle_min", "volume_cycle_required")):
        raise ValueError("La comparaison exige tailles et cadences identiques")
    candidate = replay(rows, cfg, half_spread_bps, slip_bps, "trend_hold", signal_rows=signals,
                       fixed_cadence=True, include_events=True)
    decisions = [event["decision_ts"] for event in candidate["events"] if event["decision_ts"] is not None]
    start = decisions[0] if decisions else math.inf
    baseline = replay(rows, baseline_cfg, half_spread_bps, slip_bps, "trend_hold", signal_rows=signals,
                      fixed_cadence=True, include_events=include_events, direction_policy="legacy",
                      not_before_ts=start)
    if not include_events:
        candidate.pop("events")
    return {"simulation_only": True, "market_validation": False,
            "scope": "direction_only_common_warmup_fixed_decision_clock",
            "common_start_ts": start if math.isfinite(start) else None,
            "same_order_count": baseline["estimated_orders"] == candidate["estimated_orders"],
            "same_cycle_count": baseline["cycles"] == candidate["cycles"],
            "comparison": [baseline, candidate]}


def read_rows(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as f:
        return _validated_rows((r["ts"], r["wti"], r["brent"]) for r in csv.DictReader(f))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv")
    parser.add_argument("--signal-csv", help="Independent 1m closes, ts = candle open; available at ts + 60")
    parser.add_argument("--half-spread-bps", type=float, required=True)
    parser.add_argument("--slip-bps", type=float, required=True)
    parser.add_argument("--fixed-cadence", action="store_true", help="Legacy vs adaptive at equal mandatory cadence")
    args = parser.parse_args()
    cfg = load_pair_config(ROOT / "config.toml")
    rows = read_rows(args.csv)
    signals = read_rows(args.signal_csv) if args.signal_csv else None
    if args.fixed_cadence:
        result = compare_fixed_cadence(rows, cfg, args.half_spread_bps, args.slip_bps, signal_rows=signals)
    else:
        result = {"simulation_only": True, "market_validation": False,
                  "comparison": [replay(rows, cfg, args.half_spread_bps, args.slip_bps, mode,
                                        signal_rows=signals)
                                 for mode in ("mean_reversion", "trend_hold")]}
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
