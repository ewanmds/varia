import asyncio
import json
import math

import pytest

from arb.config import PairCfg
from arb.pair_signal import TrendSignal
from arb.tv_candles import ChartDenied, TradingViewCandles, frame, parse_closes, unpack
from tests.test_pair_orchestrator import make


def loaded_feed(now, slope=1.0):
    feed = TradingViewCandles(window_min=5, clock=lambda: now)
    minute = math.floor(now / 60) * 60
    stamps = [minute - i * 60 for i in reversed(range(7))]
    feed.closes = ({t: 90 * math.exp(slope * i / 1e4) for i, t in enumerate(stamps)},
                   {t: 100 for t in stamps})
    feed.last_poll, feed.last_error = now, None
    return feed


def test_synchronizes_exact_timestamps_and_excludes_unclosed_and_future_bars():
    now = 6000.0
    feed = loaded_feed(now)
    feed.closes[0][now + 60] = 1000
    feed.closes[1][now + 60] = 1
    snapshot = feed.snapshot()
    assert snapshot["ready"]
    assert len(snapshot["points"]) == 5
    assert snapshot["points"][-1][0] == now - 60
    assert snapshot["last_closed_at"] == now
    assert snapshot["ratio"] < 1
    del feed.closes[1][now - 120]
    assert not feed.snapshot()["ready"]


def test_stale_poll_stale_candles_and_feed_error_are_never_tradable():
    feed = loaded_feed(6000)
    assert not feed.snapshot(6200)["ready"]
    feed.last_poll = 6600
    assert "anciennes" in feed.snapshot(6600)["reason"]
    feed = loaded_feed(6000)
    feed.last_error = "TradingView indisponible"
    assert not feed.snapshot()["ready"]


@pytest.mark.parametrize("slope,side", [(1.0, "B"), (-1.0, "A")])
def test_five_closed_minutes_are_ready_without_sixth_minute(slope, side):
    snapshot = loaded_feed(6000, slope).snapshot()
    signal = TrendSignal(window_min=5, confirm_min=2)
    signal.replace_closed(snapshot["points"], 6000)
    assert signal.status()["ready"] and signal.status()["side"] == side
    signal.replace_closed(snapshot["points"][1:], 6000)
    assert not signal.status()["ready"]


def test_frame_decoder_combined_heartbeat_and_bad_bars():
    heartbeat = "~h~1234"
    raw = frame("chart_create_session", ["cs_test", ""]) + f"~m~{len(heartbeat)}~m~{heartbeat}"
    assert list(unpack(raw))[1] == heartbeat
    with pytest.raises(ValueError):
        list(unpack("~m~50~m~{}"))
    with pytest.raises(ValueError):
        frame("place_order", [])
    bars = [{"v": [6000, 90, 91, 89, 90]}, {"v": [6060, 0, 0, 0, float("nan")]},
            {"v": [6070, 0, 0, 0, 91]}, {"v": []}]
    assert parse_closes(bars) == {6000: 90.0}


class Socket:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.active = None
        self.commands = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def send(self, raw):
        msg = list(unpack(raw))[0]
        self.commands.append(msg)
        if msg["m"] == "create_series":
            assert self.active is None, "Only one series is permitted"
            cs, series = msg["p"][:2]
            self.active = series
            price = 90 if series == "s0" else 100
            payloads = [
                {"m": "timescale_update", "p": [cs, {series: {"s": [{"v": [6000, price, price, price, price]}]}}]},
                {"m": "series_completed", "p": [cs, series]},
            ]
            raw = ""
            for payload in payloads:
                data = json.dumps(payload)
                raw += f"~m~{len(data)}~m~{data}"
            await self.incoming.put(raw)
        elif msg["m"] == "remove_series":
            assert self.active == msg["p"][1]
            self.active = None

    async def recv(self):
        return await self.incoming.get()


async def test_fetch_two_legs_sequentially_with_one_series_and_publishes_atomically():
    socket = Socket()
    feed = TradingViewCandles(clock=lambda: 6100, connect=lambda *a, **k: socket)
    await feed.fetch()
    assert feed.closes == ({6000: 90.0}, {6000: 100.0})
    assert socket.active is None and feed.last_error is None
    assert [m["p"][4] for m in socket.commands if m["m"] == "create_series"] == ["1", "1"]


async def test_permission_denial_stops_instead_of_retrying_or_falling_back():
    feed = TradingViewCandles()
    calls = []

    async def denied():
        calls.append(1)
        raise ChartDenied("TradingView: series_error")

    feed.fetch = denied
    stop = asyncio.Event()
    task = asyncio.create_task(feed.run(stop))
    await asyncio.sleep(0)
    assert feed.blocked and calls == [1] and not feed.snapshot()["ready"]
    stop.set()
    await task


async def tv_bot():
    cfg = PairCfg(enabled=True, signal_mode="trend_hold", trend_source="tradingview",
                  trend_window_min=5, trend_confirm_min=2, leg_usd=1000, tranche_usd=500,
                  order_gap_s=0)
    o, var, clock = await make(cfg=cfg)
    o.tv = loaded_feed(clock.t)
    return o, var, clock


async def test_tradingview_trend_controls_direction_while_var_prices_execute():
    o, var, clock = await tv_bot()
    # Contradictory local history must not replace the TradingView decision.
    for i in range(10):
        o.trend.add(clock.t - (10 - i) * 60, 90 * math.exp(-i / 1e3), 100)
    await o.tick()
    assert var.pos["USOILP"] > 0 > var.pos["UKOILP"]
    pair = o.status()["pair"]
    assert pair["signal_ratio"] != pair["ratio"]
    assert pair["trend_source"] == "tradingview" and pair["tradingview"]["ready"]


async def test_stale_tradingview_blocks_entry_but_never_blocks_loss_exit():
    o, var, clock = await tv_bot()
    o.tv.last_error = "flux TradingView muet"
    await o.tick()
    assert not var.orders and o.status()["pair"]["state"] == "ATTENTE"
    o.tv = loaded_feed(clock.t)
    clock.t += 31
    await o.tick()
    assert var.pos["USOILP"] > 0
    o.tv.last_error = "flux TradingView muet"
    o.journal._inner.daily = -201
    await o.tick()
    assert o.killed and var.pos["USOILP"] == var.pos["UKOILP"] == 0


async def test_explicit_manual_entry_is_once_only_without_fabricated_tv_signal():
    o, var, clock = await tv_bot()
    o.cfg.cycle_min = 1
    o.tv.last_error = "chargement TradingView"
    o.request_entry("B")
    await o.tick()
    assert var.pos["USOILP"] > 0 > var.pos["UKOILP"] and len(var.orders) == 4
    assert o._manual_entry_side is None and o.status()["pair"]["manual_entry_executed"] == "B"
    assert not o.status()["pair"]["trend"]["ready"]
    clock.t += 61
    await o.tick()
    assert len(var.orders) == 4


async def test_manual_entry_never_overrides_daily_loss_guard():
    o, var, _ = await tv_bot()
    o.journal._inner.daily = -201
    o.request_entry("B")
    await o.tick()
    assert o.killed and not var.orders


async def test_one_minute_controls_resume_normal_trend_after_manual_entry():
    o, var, clock = await tv_bot()
    o.cfg.cycle_min = 1
    o.request_entry("B")
    await o.tick()
    clock.t += 61
    o.tv = loaded_feed(clock.t, slope=-1)
    await o.tick()
    assert var.pos["USOILP"] < 0 < var.pos["UKOILP"] and len(var.orders) == 12


async def test_stale_tradingview_blocks_scheduled_volume_cycle():
    o, var, clock = await tv_bot()
    o.cfg.volume_cycle_min = 5
    o.cfg.volume_cycle_required = True
    o.request_entry("B")
    await o.tick()
    o.tv.last_error = "flux TradingView muet"
    clock.t += 301
    await o.tick()
    assert len(var.orders) == 4 and var.pos["USOILP"] > 0


@pytest.mark.parametrize("kwargs", [{"trend_source": "unknown"}, {"tv_stale_s": 600},
                                    {"tv_brent_symbol": "TVC:USOIL"},
                                    {"trend_source": "tradingview", "warmup_entry_side": "B"}])
def test_invalid_source_config_rejected(kwargs):
    with pytest.raises(ValueError):
        PairCfg(**kwargs)
