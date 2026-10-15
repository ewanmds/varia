"""Only a post-close read can publish a closed candle for either TV leg."""
import asyncio
import json

import pytest

from arb.tv_candles import TradingViewCandles, unpack


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


class Socket:
    def __init__(self, closes, after_first_leg=None):
        self.closes = closes
        self.after_first_leg = after_first_leg
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
            assert self.active is None
            session, series = msg["p"][:2]
            self.active = series
            index = int(series[1:])
            rows = [{"v": [ts, price, price, price, price]} for ts, price in self.closes[index].items()]
            payloads = [
                {"m": "timescale_update", "p": [session, {series: {"s": rows}}]},
                {"m": "series_completed", "p": [session, series]},
            ]
            encoded = []
            for payload in payloads:
                data = json.dumps(payload)
                encoded.append(f"~m~{len(data)}~m~{data}")
            await self.incoming.put("".join(encoded))
        elif msg["m"] == "remove_series":
            assert self.active == msg["p"][1]
            self.active = None
            if msg["p"][1] == "s0" and self.after_first_leg is not None:
                self.after_first_leg()

    async def recv(self):
        return await self.incoming.get()


def bars(partial_wti=20):
    stamps = range(5700, 6061, 60)
    wti = {ts: 90 + i * 0.01 for i, ts in enumerate(stamps)}
    brent = {ts: 100 for ts in stamps}
    wti[6000] = partial_wti
    wti[6060] = 1  # Future/current values must also stay out of the publication.
    return wti, brent


async def test_fetch_straddling_minute_end_excludes_partial_bar_from_both_legs():
    clock = Clock(6059.75)

    def first_leg_completed():
        clock.now = 6060.25

    socket = Socket(bars(), first_leg_completed)
    feed = TradingViewCandles(window_min=5, clock=clock, connect=lambda *a, **k: socket)
    await feed.fetch()
    assert feed.last_poll == 6060.25
    assert all(6000 not in leg and 6060 not in leg for leg in feed.closes)
    assert feed.snapshot()["points"][-1][0] == 5940
    assert feed.snapshot()["ready"]
    assert len([m for m in socket.commands if m["m"] == "create_series"]) == 2


async def test_cached_partial_never_promotes_without_a_postclose_poll():
    clock = Clock(6050)
    sockets = [Socket(bars(partial_wti=20)), Socket(bars(partial_wti=91))]
    feed = TradingViewCandles(window_min=5, clock=clock,
                             connect=lambda *a, **k: sockets.pop(0))
    await feed.fetch()
    assert feed.last_poll == 6050 and feed.snapshot()["last_closed_at"] == 6000
    clock.now = 6061
    snapshot = feed.snapshot()
    assert snapshot["last_closed_at"] == 6000
    assert 6000 not in [point[0] for point in snapshot["points"]]
    clock.now = 6091
    assert not feed.snapshot()["ready"]
    assert feed.snapshot()["reason"] == "bougies TradingView 1m trop anciennes"
    await feed.fetch()
    snapshot = feed.snapshot()
    assert snapshot["ready"] and snapshot["last_closed_at"] == 6060
    assert snapshot["points"][-1] == (6000, 91, 100)
    assert snapshot["last_poll_at"] == 6091  # Actual publication, rather than ts + 60.
    assert 6060 not in feed.closes[0]


async def test_poll_beginning_exactly_at_close_can_publish_that_closed_bar():
    clock = Clock(6060)
    socket = Socket(bars(partial_wti=91))
    feed = TradingViewCandles(window_min=5, clock=clock, connect=lambda *a, **k: socket)
    await feed.fetch()
    assert feed.snapshot()["points"][-1] == (6000, 91, 100)
    assert 6060 not in feed.closes[0]


async def test_partial_only_reply_does_not_replace_previous_atomic_publication():
    clock = Clock(6059)
    socket = Socket(({6000: 20}, {6000: 100}))
    feed = TradingViewCandles(window_min=5, clock=clock, connect=lambda *a, **k: socket)
    previous = ({5940: 90}, {5940: 100})
    feed.closes = previous
    feed.last_poll = 6040
    with pytest.raises(ValueError, match="No TradingView minute bars"):
        await feed.fetch()
    assert feed.closes is previous and feed.last_poll == 6040
