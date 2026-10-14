"""Read-only TradingView guest chart transport for synchronized 1-minute closes.

This is an unofficial chart protocol, not a supported market-data API. Only
public guest-accessible individual symbols are read. A single series is used
sequentially; permission/protocol errors stop the feed rather than bypassing
restrictions. No cookies, account tokens or orders are involved.
"""
from __future__ import annotations

import asyncio
import json
import math
import secrets
import time

URL = "wss://data.tradingview.com/socket.io/websocket"
ALLOWED = {"set_auth_token", "chart_create_session", "resolve_symbol", "create_series", "remove_series"}


class ChartDenied(RuntimeError):
    pass


def frame(method, params):
    if method not in ALLOWED:
        raise ValueError("Only read-only chart methods are allowed")
    payload = json.dumps({"m": method, "p": params}, separators=(",", ":"), ensure_ascii=True)
    return f"~m~{len(payload)}~m~{payload}"


def unpack(raw):
    if not isinstance(raw, str):
        raise ValueError("Invalid chart frame")
    while raw:
        if not raw.startswith("~m~"):
            raise ValueError("Invalid chart frame prefix")
        size, rest = raw[3:].split("~m~", 1)
        size = int(size)
        if size < 0 or size > len(rest):
            raise ValueError("Incomplete chart frame")
        payload, raw = rest[:size], rest[size:]
        yield payload if payload.startswith("~h~") else json.loads(payload)


def parse_closes(rows):
    closes = {}
    for row in rows:
        try:
            values = row["v"]
            ts, close = float(values[0]), float(values[4])
            if math.isfinite(ts) and math.isfinite(close) and ts >= 0 and ts % 60 == 0 and close > 0:
                closes[int(ts)] = close
        except (KeyError, IndexError, TypeError, ValueError):
            continue
    return closes


class TradingViewCandles:
    def __init__(self, wti="TVC:USOIL", brent="TVC:UKOIL", *, window_min=5,
                 stale_s=90.0, clock=time.time, connect=None, poll_s=30.0):
        self.wti, self.brent = wti, brent
        self.window_min, self.stale_s = window_min, stale_s
        self.clock, self.connect, self.poll_s = clock, connect, poll_s
        self.closes = ({}, {})
        self.last_poll = None
        self.last_error = "chargement des bougies TradingView 1m"
        self.blocked = False

    async def fetch(self):
        from websockets.asyncio.client import connect
        connector = self.connect or connect
        async with connector(URL, origin="https://www.tradingview.com", open_timeout=10,
                             close_timeout=3, max_size=2**20) as ws:
            session = "cs_" + secrets.token_hex(6)
            await ws.send(frame("set_auth_token", ["unauthorized_user_token"]))
            await ws.send(frame("chart_create_session", [session, ""]))
            result = []
            for i, symbol in enumerate((self.wti, self.brent)):
                series, reference = f"s{i}", f"symbol_{i}"
                definition = "=" + json.dumps({"symbol": symbol, "adjustment": "splits", "session": "regular"})
                await ws.send(frame("resolve_symbol", [session, reference, definition]))
                await ws.send(frame("create_series", [session, series, series, reference, "1", self.window_min + 5]))
                closes = {}
                async with asyncio.timeout(15):
                    complete = False
                    while not complete:
                        for msg in unpack(await ws.recv()):
                            if isinstance(msg, str):
                                await ws.send(f"~m~{len(msg)}~m~{msg}")
                                continue
                            method, params = msg.get("m"), msg.get("p", [])
                            if method in ("series_error", "critical_error", "protocol_error", "symbol_error"):
                                raise ChartDenied(f"TradingView: {method}")
                            if method == "timescale_update" and params and params[0] == session:
                                closes.update(parse_closes(params[1].get(series, {}).get("s", [])))
                            if method == "series_completed" and params[:2] == [session, series]:
                                complete = True
                if not closes:
                    raise ValueError("No TradingView minute bars")
                result.append(closes)
                await ws.send(frame("remove_series", [session, series]))
            # Publish atomically: never combine a new leg with an old snapshot.
            self.closes = tuple(result)
            self.last_poll = self.clock()
            self.last_error = None

    def snapshot(self, now=None):
        now = self.clock() if now is None else now
        a, b = self.closes
        common = sorted(t for t in a.keys() & b.keys() if t + 60 <= now)
        points = [(t, a[t], b[t]) for t in common[-self.window_min:]]
        reason = self.last_error
        if not reason and (self.last_poll is None or now - self.last_poll > self.stale_s):
            reason = "flux TradingView 1m muet"
        if not reason and len(points) < self.window_min:
            reason = "préchauffage TradingView 1m : historique insuffisant"
        if not reason and now - (points[-1][0] + 60) > self.stale_s:
            reason = "bougies TradingView 1m trop anciennes"
        if not reason and any(q[0] - p[0] != 60 for p, q in zip(points, points[1:])):
            reason = "bougies TradingView 1m discontinues"
        return {"ready": reason is None, "reason": reason, "points": points,
                "ratio": points[-1][1] / points[-1][2] if points else None,
                "last_closed_at": points[-1][0] + 60 if points else None,
                "last_poll_at": self.last_poll, "symbols": [self.wti, self.brent], "timeframe": "1m"}

    async def run(self, stop):
        failures = 0
        while not stop.is_set():
            try:
                await self.fetch()
                failures = 0
            except ChartDenied as exc:
                self.blocked = True
                self.last_error = str(exc)
                await stop.wait()
                return
            except Exception as exc:
                failures += 1
                self.last_error = f"TradingView indisponible ({type(exc).__name__})"
            delay = min(60, 5 * 2**min(failures, 4)) if failures else self.poll_s
            try:
                await asyncio.wait_for(stop.wait(), delay)
            except asyncio.TimeoutError:
                pass
