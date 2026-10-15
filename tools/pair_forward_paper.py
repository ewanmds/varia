"""Prospective public-data paper comparison. Never connects to an account or submits an order.

Only VarPriceStream and TradingViewCandles connect to public market data.
SyntheticQuoteSource computes quotes locally from fresh observed Var marks;
PaperPairVar.market/status and every fill are local simulations. Spread and
slippage are REQUIRED assumptions, not measured execution costs.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from arb.config import PairCfg
from arb.pair_exec import PairExecutor, plan_cycle, target_usd
from arb.pair_paper import PaperPairVar
from arb.pair_signal import TrendSignal, select_volume_side
from arb.pricing import VarQuote
from arb.tv_candles import ChartDenied, TradingViewCandles
from arb.var_ws import VarPriceStream

ASSETS = {"wti": "USOILP", "brent": "UKOILP"}


class Capture:
    def __init__(self, out=None):
        self.out = Path(out) if out is not None else None
        self.files = {}
        if self.out:
            self.out.mkdir(parents=True, exist_ok=True)

    def event(self, kind, record):
        if self.out is None:
            return
        if kind not in self.files:
            self.files[kind] = (self.out / f"{kind}.jsonl").open("a", encoding="utf-8")
        self.files[kind].write(json.dumps(record, allow_nan=False, separators=(",", ":")) + "\n")

    def document(self, name, record):
        if self.out is None:
            return
        temporary = self.out / (name + ".tmp")
        temporary.write_text(json.dumps(record, allow_nan=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.out / name)

    def flush(self):
        for file in self.files.values():
            file.flush()

    def close(self):
        for file in self.files.values():
            file.close()


class SyntheticQuoteSource:
    """Quote-only local object: deliberately has no market/account/network method."""
    def __init__(self, half_spread_bps, slip_bps, *, clock=time.time, max_age_s=5):
        if not all(math.isfinite(v) and 0 <= v < 1e4 for v in (half_spread_bps, slip_bps)):
            raise ValueError("spread/slippage must be finite nonnegative assumptions")
        if half_spread_bps + slip_bps >= 1e4:
            raise ValueError("total assumed cost must be below 10000 bps")
        self.half_spread_bps, self.slip_bps = half_spread_bps, slip_bps
        self.clock, self.max_age_s = clock, max_age_s
        self.marks = {}
        self.n = 0

    def observe(self, asset, update):
        mark, received = float(update["mark"]), float(update["ts_recv_ms"]) / 1000
        if asset not in ASSETS.values() or not math.isfinite(mark) or mark <= 0 or not math.isfinite(received):
            return
        self.marks[asset] = {"mark": mark, "received_at": received,
                             "server_ms": update.get("ts_server_ms")}

    def prices(self, now=None):
        now = self.clock() if now is None else now
        result = {}
        for leg, asset in ASSETS.items():
            update = self.marks.get(asset)
            if not update or not 0 <= now - update["received_at"] <= self.max_age_s:
                raise ValueError("fresh public Var marks unavailable")
            result[leg] = update["mark"]
        return result

    async def quote(self, asset, kind, qty, *args, **kwargs):
        leg = next((leg for leg, name in ASSETS.items() if name == asset), None)
        if leg is None:
            raise ValueError("asset outside public paper pair")
        mark = self.prices()[leg]
        width = (self.half_spread_bps + self.slip_bps) / 1e4
        self.n += 1
        return VarQuote(mark * (1 - width), mark * (1 + width), float(qty), self.clock(), f"local-{self.n}")


class PaperLedger:
    def __init__(self, name, source, capture, clock):
        self.name, self.source, self.capture, self.clock = name, source, capture, clock
        self.cash = self.volume = self.assumed_cost = 0.0
        self.orders = 0
        self.qty = dict.fromkeys(ASSETS, 0.0)

    def record_incident(self, *args):
        self.capture.event("events", {"kind": "paper_incident", "observed_at": self.clock(),
                                      "policy": self.name, "message": str(args)})

    def record_arb(self, *args):
        return self.record_pair_fill(*args)

    def record_pair_fill(self, *args, **evidence):
        asset, side, px, qty = args[0], args[5], float(args[6]), float(args[7])
        leg = next(leg for leg, name in ASSETS.items() if name == asset)
        delta = qty if side == "buy" else -qty
        self.cash -= delta * px
        self.qty[leg] = round(self.qty[leg] + delta, 9)
        self.volume += qty * px
        self.orders += 1
        mid = evidence.get("quote_mid")
        cost = qty * abs(px - mid) if mid is not None else None
        if cost is not None:
            self.assumed_cost += cost
        self.capture.event("fills", {"observed_at": self.clock(), "policy": self.name,
            "scope": "local_simulation_only", "asset": asset, "side": side, "qty": qty, "price": px,
            "quote_mid": mid, "quote_side_px": evidence.get("quote_px"),
            "reduce_only": evidence.get("reduce_only"), "assumed_transaction_cost_usd": cost,
            "execution_cost_verified": False})
        return self.orders

    def summary(self, prices=None):
        pnl = self.cash + sum(self.qty[leg] * prices[leg] for leg in ASSETS) if prices else None
        if all(abs(qty) < 1e-8 for qty in self.qty.values()):
            pnl = self.cash
        return {"orders": self.orders, "volume_usd": self.volume, "cash_usd": self.cash,
                "positions_qty": dict(self.qty), "net_paper_pnl_usd": pnl,
                "paper_cost_per_m": -pnl / self.volume * 1e6 if pnl is not None and self.volume else None,
                "assumed_transaction_cost_usd": self.assumed_cost}


class ForwardPaper:
    def __init__(self, source, *, capture=None, clock=time.time, sleep=asyncio.sleep):
        self.source, self.capture, self.clock = source, capture or Capture(), clock
        self.cfg = PairCfg(enabled=True, signal_mode="trend_hold", trend_window_min=5,
            trend_confirm_min=2, trend_context_min=30, leg_usd=1000, tranche_usd=500,
            cycle_min=1, volume_cycle_min=5, volume_cycle_required=True, order_gap_s=1)
        self.arms = {}
        for name in ("legacy_fast5", "adaptive_context30"):
            paper = PaperPairVar(source, self.cfg)
            ledger = PaperLedger(name, source, self.capture, clock)
            self.arms[name] = {"paper": paper, "ledger": ledger,
                "executor": PairExecutor(paper, self.cfg, ledger, clock=clock, sleep=sleep),
                "side": None, "cycles": 0}
        self.publication = None
        self.pending = None
        self.next_cycle_at = 0.0
        self.last_good_at = None
        self.common_start_at = None
        self.failure = None

    def observe_tv(self, snapshot, observed_at):
        # Availability is the actual received publication, never historical bar close time.
        copied = {**snapshot, "points": [tuple(point) for point in snapshot.get("points", [])],
                  "observed_at": observed_at}
        if any(point[0] + 60 > observed_at for point in copied["points"]):
            copied.update(ready=False, reason="unclosed candle in public publication")
        self.publication = copied
        self.capture.event("signals", {"observed_at": observed_at, "ready": copied.get("ready", False),
            "reason": copied.get("reason"), "fetch_started_at": copied.get("fetch_started_at"),
            "bars": [{"bar_open_ts": ts, "wti": wti, "brent": brent} for ts, wti, brent in copied["points"]]})

    def decision(self, now):
        publication = self.publication
        if not publication or not publication.get("ready"):
            raise ValueError("published TradingView closed history unavailable")
        if not 0 <= now - publication["observed_at"] <= 90:
            raise ValueError("published TradingView observation stale")
        points = publication["points"]
        if not points or now - (points[-1][0] + 60) > 90:
            raise ValueError("published TradingView candles stale")
        prices = self.source.prices(now)
        signals = []
        for context in (0, 30):
            trend = TrendSignal(5, 2, context_min=context)
            trend.replace_closed(points, now)
            signal = trend.status()
            if not signal["ready"]:
                raise ValueError(signal["reason"])
            signals.append(signal)
        legacy, adaptive = signals
        old_side = self.arms["legacy_fast5"]["side"]
        slope = legacy["slope_bps_min"] or 0
        desired = {"legacy_fast5": legacy["side"] or old_side or ("A" if slope < 0 else "B"),
            "adaptive_context30": select_volume_side(adaptive, self.arms["adaptive_context30"]["side"])}
        return desired, prices

    async def advance(self, now=None):
        now = self.clock() if now is None else now
        if self.failure:
            return False
        try:
            desired, prices = self.decision(now)
        except ValueError as exc:
            if self.pending:
                self.capture.event("events", {"kind": "pending_deferred", "observed_at": now, "reason": str(exc)})
            self.pending = None
            return False
        self.last_good_at = now
        if self.pending is None:
            if now < self.next_cycle_at:
                return False
            self.common_start_at = now if self.common_start_at is None else self.common_start_at
            self.pending = {"decision_at": now, "eligible_at": now + 1, "desired": desired,
                            "tv_observed_at": self.publication["observed_at"]}
            self.capture.event("decisions", {**self.pending, "var_prices": prices,
                "last_bar_open_ts": self.publication["points"][-1][0]})
            return False
        if now < self.pending["eligible_at"]:
            return False
        if any(self.source.marks[asset]["received_at"] < self.pending["eligible_at"]
               for asset in ASSETS.values()):
            # Elapsed wall time alone is not a new market observation.
            return False
        if desired != self.pending["desired"]:
            self.capture.event("events", {"kind": "direction_changed_before_execution", "observed_at": now})
            self.pending = None
            return False
        pending, self.pending = self.pending, None

        def guard():
            try:
                current, _ = self.decision(self.clock())
                return None if current == pending["desired"] else "public direction changed before opening"
            except ValueError as exc:
                return str(exc)

        async def run_arm(name, arm):
            raw = await arm["paper"].positions()
            current = {}
            for leg, asset in ASSETS.items():
                qty = (raw.get(asset) or {}).get("qty", 0.0)
                current[leg] = math.copysign(max(1, round(abs(qty) * prices[leg] / 500)) * 500, qty) if qty else 0.0
            side = pending["desired"][name]
            steps = plan_cycle(current, target_usd(side, 1000), 500, reopen=side == arm["side"])
            result = await arm["executor"].run_steps(steps, prices, entry_guard=guard)
            ended = self.clock()
            if result.ok:
                arm["side"], arm["cycles"] = side, arm["cycles"] + 1
            return name, result, ended

        completed = await asyncio.gather(*(run_arm(name, arm) for name, arm in self.arms.items()))
        for name, result, ended in completed:
            self.capture.event("cycles", {"policy": name, "decision_at": pending["decision_at"],
                "execution_started_at": now, "ended_at": ended, "ok": result.ok,
                "deferred": result.deferred, "reason": result.reason, "fills": len(result.filled)})
            if not result.ok:
                self.failure = f"paper execution incomplete: {name}: {result.reason}"
        self.next_cycle_at = max(ended for _, _, ended in completed) + 300
        return True

    async def flatten(self):
        results = {}
        for name, arm in self.arms.items():
            raw = await arm["paper"].positions()
            if not raw:
                results[name] = {"ok": True, "already_flat": True}
                continue
            try:
                prices = self.source.prices()
                current = {}
                for leg, asset in ASSETS.items():
                    qty = (raw.get(asset) or {}).get("qty", 0.0)
                    current[leg] = (math.copysign(max(1, round(abs(qty) * prices[leg] / 500)) * 500, qty)
                                    if qty else 0.0)
                steps = plan_cycle(current, dict.fromkeys(ASSETS, 0.0), 500, reopen=True)
                result = await arm["executor"].run_steps(steps, prices)
                results[name] = {"ok": result.ok, "reason": result.reason}
            except Exception as exc:
                results[name] = {"ok": False, "reason": type(exc).__name__ + ": " + str(exc)}
        return results

    def status(self, now=None):
        now = self.clock() if now is None else now
        try:
            prices = self.source.prices(now)
        except ValueError:
            prices = None
        arms = {name: {**arm["ledger"].summary(prices), "cycles": arm["cycles"]}
                for name, arm in self.arms.items()}
        return {"observed_at": now, "scope": "prospective_public_dual_source_paper_only",
            "real_orders_sent": 0, "account_connection": False, "executable_cpm_validated": False,
            "cost_assumptions": {"half_spread_bps": self.source.half_spread_bps,
                "slip_bps": self.source.slip_bps, "application": "sum applied as synthetic half quote width"},
            "common_start_at": self.common_start_at, "next_cycle_at": self.next_cycle_at,
            "tv_observed_at": (self.publication or {}).get("observed_at"),
            "fresh_var_marks": prices is not None, "failure": self.failure,
            "same_order_count": len({arm["orders"] for arm in arms.values()}) == 1,
            "same_cycle_count": len({arm["cycles"] for arm in arms.values()}) == 1, "policies": arms,
            "limitations": ["Spread/slippage assumed, not observed RFQ costs.",
                "No session, funding, margin or real-order failure model.",
                "Observed Var public marks are not executable bid/ask quotes."]}


async def run(args):
    capture = Capture(args.out)
    source = SyntheticQuoteSource(args.half_spread_bps, args.slip_bps)
    engine = ForwardPaper(source, capture=capture)
    stop = asyncio.Event()
    started = time.time()
    denied = False
    last_error = None

    def on_var(asset, update):
        source.observe(asset, update)
        capture.event("var", {"asset": asset, "observed_at": update["ts_recv_ms"] / 1000,
            "server_ms": update.get("ts_server_ms"), "mark": update["mark"]})

    var = VarPriceStream({asset: "commodity" for asset in ASSETS.values()}, on_update=on_var)
    tv = TradingViewCandles(window_min=30)

    async def tv_worker():
        nonlocal denied, last_error
        while not stop.is_set():
            began = time.time()
            try:
                await asyncio.wait_for(tv.fetch(), 50)
                observed = time.time()
                a, b = tv.closes
                common = sorted(ts for ts in a.keys() & b.keys() if ts + 60 <= began)[-30:]
                points = [(ts, a[ts], b[ts]) for ts in common]
                ready = (len(points) == 30 and observed - (points[-1][0] + 60) <= 90
                         and all(q[0] - p[0] == 60 for p, q in zip(points, points[1:])))
                engine.observe_tv({"points": points, "ready": ready,
                    "reason": None if ready else "public closed history incomplete/stale/discontinuous",
                    "fetch_started_at": began}, observed)
                last_error = None
            except ChartDenied:
                denied, last_error = True, "TradingView ChartDenied; no retry/bypass"
                capture.event("events", {"kind": "public_feed_denied", "observed_at": time.time(), "source": "TradingView"})
                return
            except Exception as exc:
                last_error = "TradingView fetch failed: " + type(exc).__name__
                engine.observe_tv({"points": [], "ready": False, "reason": last_error}, time.time())
                capture.event("events", {"kind": "public_feed_failure", "observed_at": time.time(), "reason": last_error})
            try:
                await asyncio.wait_for(stop.wait(), 30 if last_error is None else 5)
            except asyncio.TimeoutError:
                pass

    tasks = [asyncio.create_task(var.run(stop)), asyncio.create_task(tv_worker())]
    reason = "duration_complete"
    flatten = {}
    try:
        while time.time() - started < args.minutes * 60:
            await engine.advance()
            status = engine.status()
            status.update(started_at=started, requested_minutes=args.minutes, last_public_error=last_error)
            capture.document("status.json", status)
            capture.flush()
            if denied:
                reason = "public_feed_denied"
                break
            if engine.failure:
                reason = engine.failure
                break
            if time.time() - (engine.last_good_at or started) >= 60:
                reason = "public_data_unavailable_60s"
                break
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        reason = "cancelled"
        raise
    finally:
        try:
            flatten = await asyncio.wait_for(engine.flatten(), 30)
        except Exception as exc:
            flatten = {"error": type(exc).__name__}
        stop.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        final = {**engine.status(), "started_at": started, "ended_at": time.time(),
                 "stop_reason": reason, "flatten_paper_only": flatten}
        capture.document("summary.json", final)
        capture.document("status.json", {**final, "finished": True})
        capture.flush()
        capture.close()
        print(json.dumps(final, allow_nan=False), flush=True)
    return final


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--minutes", type=float, default=15)
    parser.add_argument("--half-spread-bps", type=float, required=True)
    parser.add_argument("--slip-bps", type=float, required=True)
    parser.add_argument("--out", type=Path, required=True, help="Ignored output directory under repository data/")
    args = parser.parse_args()
    if not math.isfinite(args.minutes) or not 0 < args.minutes <= 1440:
        parser.error("minutes must be between 0 and 1440")
    args.out = args.out.resolve()
    if not args.out.is_relative_to((ROOT / "data").resolve()):
        parser.error("output must stay under ignored repository data/")
    SyntheticQuoteSource(args.half_spread_bps, args.slip_bps)
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
