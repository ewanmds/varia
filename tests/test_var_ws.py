import asyncio
import json

import pytest

from arb.var_ws import ForbiddenSend, VarPriceStream, channel_key, parse_ts_ms

ASSETS = {"US100S": "index", "US500S": "index", "XAUS": "commodity",
          "USOILP": "commodity", "UKOILP": "commodity"}


def price_msg(chan, px, ts="2026-09-23T14:54:39.875273Z"):
    return json.dumps({"channel": "instrument_price:" + chan,
                       "pricing": {"price": str(px), "underlying_price": str(px), "native_price": "1",
                                   "delta": "1", "timestamp": ts}})


class FakeWS:
    def __init__(self, incoming):
        self.incoming = list(incoming)
        self.sent = []
        self.closed = False

    async def send(self, s):
        self.sent.append(json.loads(s))

    async def close(self):
        self.closed = True

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.incoming:
            raise StopAsyncIteration
        return self.incoming.pop(0)


def test_channel_keys():
    assert channel_key("USOILP", "commodity") == "C-CMD-USOILP-USDC-0"
    assert channel_key("US100S", "index") == "C-IDX-US100S-USDC-0"


def test_parse_ts_ms_nanoseconds():
    assert parse_ts_ms("1970-01-01T00:00:01.256963825Z") == 1256
    assert parse_ts_ms("1970-01-01T00:00:02Z") == 2000
    assert parse_ts_ms(None) is None
    assert parse_ts_ms("garbage") is None


def test_handle_price_updates_latest_and_callback():
    got = []
    s = VarPriceStream(ASSETS, on_update=lambda a, u: got.append((a, u)), clock_ms=lambda: 42)
    s.handle_raw(price_msg("C-CMD-USOILP-USDC-0", 91.06, "1970-01-01T00:00:01.5Z"))
    u = s.latest("USOILP")
    assert u["mark"] == 91.06 and u["bid"] is None and u["ask"] is None
    assert u["ts_recv_ms"] == 42 and u["ts_server_ms"] == 1500
    assert got == [("USOILP", u)]
    assert s.latest("UKOILP") is None


def test_ignores_heartbeat_unknown_channel_and_bad_price():
    s = VarPriceStream(ASSETS, clock_ms=lambda: 1)
    assert s.handle_raw(json.dumps({"type": "heartbeat", "timestamp": "x"})) is None
    assert s.handle_raw(price_msg("C-CMD-XAGS-USDC-0", 70)) is None
    assert s.handle_raw(price_msg("C-CMD-USOILP-USDC-0", "abc")) is None
    assert s.handle_raw("not json") is None
    assert s.latest("USOILP") is None


async def test_run_once_subscribes_to_the_five_swaps_only():
    ws = FakeWS([json.dumps({"type": "heartbeat"}), price_msg("C-IDX-US500S-USDC-0", 6700.5)])

    async def connect(url):
        return ws

    s = VarPriceStream(ASSETS, connect=connect, clock_ms=lambda: 7)
    await s.run_once()
    assert len(ws.sent) == 1
    msg = ws.sent[0]
    assert msg["action"] == "subscribe"
    assert [i["underlying"] for i in msg["instruments"]] == list(ASSETS)
    assert all(i["instrument_type"] == "swap" and i["settlement_asset"] == "USDC"
               and i["funding_interval_s"] == 0 for i in msg["instruments"])
    assert s.latest("US500S")["mark"] == 6700.5
    assert ws.closed


async def test_send_guard_rejects_anything_else():
    s = VarPriceStream(ASSETS)
    s._ws = FakeWS([])
    for bad in ({"action": "new_order"}, {"method": "post"}, {"action": "subscribe", "qty": 1}):
        with pytest.raises(ForbiddenSend):
            await s._send(bad)
    await s._send({"action": "ping"})
    await s.unsubscribe()
    assert [m["action"] for m in s._ws.sent] == ["ping", "unsubscribe"]


async def test_run_reconnects_until_stop():
    stop = asyncio.Event()
    calls = []

    async def connect(url):
        calls.append(url)
        if len(calls) == 1:
            raise OSError("boom")
        stop.set()
        return FakeWS([])

    s = VarPriceStream(ASSETS, connect=connect, reconnect_s=0.01)
    await asyncio.wait_for(s.run(stop), 2)
    assert len(calls) == 2 and s.connects == 1
