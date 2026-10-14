from __future__ import annotations

import re
import time
from typing import Optional

from arb.pricing import VarQuote
from arb.var_errors import VarError, VarRateLimited, VarRejected  # noqa: F401 (réexportées)
from arb.var_limiter import EXIT, HEDGE, QUOTE, READ, VarLimiter

_FETCH_JS = """
async ([method, path, body]) => {
  const init = { method, credentials: 'include', headers: { 'Content-Type': 'application/json' } };
  if (body !== null) init.body = JSON.stringify(body);
  const r = await fetch(path, init);
  let j = null; try { j = await r.json(); } catch (e) {}
  return [r.status, j];
}
"""


_MAX_LEV_RE = re.compile(r'"max"\s*:\s*Number\(\s*([0-9.]+)\s*\)|"max"\s*:\s*([0-9.]+)')


def parse_max_leverage(body) -> Optional[int]:
    """L1 : levier maximal lu dans le 422 de set_leverage. Format observé le 23/09 :
    {'error_message': 'leverage: Validation error: range [{"max": Number(5.0)}]'}."""
    if body is None:
        return None
    text = body.get("error_message", "") if isinstance(body, dict) else str(body)
    m = _MAX_LEV_RE.search(str(text))
    if not m:
        return None
    try:
        return int(float(m.group(1) or m.group(2)))
    except (TypeError, ValueError):
        return None


def swap_instrument(asset: str, kind: str) -> dict:
    return {"instrument_type": "swap", "underlying": asset, "settlement_asset": "USDC",
            "kind": kind, "funding_interval_s": 0}


def format_qty(qty: float) -> str:
    s = f"{qty:.8f}".rstrip("0").rstrip(".")
    return s or "0"


class CdpTransport:
    """Appels fetch() exécutés dans l'onglet Omni du Chrome dédié (session WalletConnect)."""

    def __init__(self, port: int) -> None:
        self.port = port
        self._pw = None
        self._browser = None
        self._page = None

    async def connect(self) -> None:
        from playwright.async_api import async_playwright
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.connect_over_cdp(f"http://127.0.0.1:{self.port}")
        pages = [p for c in self._browser.contexts for p in c.pages]
        page = next((p for p in pages if "variational.io" in p.url), None)
        if page is None:
            raise VarError("aucun onglet omni.variational.io ouvert dans le Chrome dédié")
        self._page = page

    async def call(self, method: str, path: str, body: Optional[dict] = None):
        if self._page is None:
            await self.connect()
        status, data = await self._page.evaluate(_FETCH_JS, [method, path, body])
        return int(status), data

    async def close(self) -> None:
        if self._pw is not None:
            await self._pw.stop()


class VarClient:
    def __init__(self, transport, limiter: VarLimiter) -> None:
        self.transport = transport
        self.limiter = limiter
        # I6 : dernier état connu de la session, renvoyé tel quel par health() sur un 429/418.
        self._health_ok = True

    async def _req(self, prio: int, method: str, path: str, body: Optional[dict] = None,
                   deadline: Optional[float] = None):
        # I1 : `deadline` (time.monotonic) -> VarRateLimited immédiat si l'attente du limiteur le
        # dépasse, au lieu de dormir au-delà du budget de couverture.
        if deadline is None:
            await self.limiter.acquire(prio)
        else:
            await self.limiter.acquire(prio, deadline=deadline)
        status, data = await self.transport.call(method, path, body)
        if status in (418, 429):
            wait = 5.0
            if isinstance(data, dict):
                try:
                    wait = float(data.get("wait_until_seconds", wait))
                except (TypeError, ValueError):
                    pass
            self.limiter.record_limited(wait)
            raise VarRateLimited(wait)
        if status != 200:
            if status in (401, 403):
                self._health_ok = False
            raise VarRejected(status, data)
        if method == "GET" and path in ("/api/me", "/api/positions", "/api/portfolio"):
            self._health_ok = True
        return data

    async def quote(self, asset: str, kind: str, qty: float, prio: int = QUOTE,
                    deadline: Optional[float] = None) -> VarQuote:
        d = await self._req(prio, "POST", "/api/quotes/indicative",
                            {"instrument": swap_instrument(asset, kind), "qty": format_qty(qty)},
                            deadline=deadline)
        return VarQuote(bid=float(d["bid"]), ask=float(d["ask"]), qty=float(d.get("qty", qty)),
                        ts=time.time(), quote_id=str(d["quote_id"]))

    async def market(self, quote_id: str, side: str, max_slippage: float,
                     reduce_only: bool = False, prio: int = HEDGE, deadline: Optional[float] = None) -> str:
        body = {"quote_id": quote_id, "side": side, "max_slippage": max_slippage}
        path = "/api/orders/new/market"
        if reduce_only:
            body["is_reduce_only"] = True
            path = "/api/quotes/accept"
        d = await self._req(prio, "POST", path, body, deadline=deadline)
        return str(d["rfq_id"])

    async def order_status(self, rfq_id: str, prio: int = READ, deadline: Optional[float] = None) -> Optional[dict]:
        eff_prio = prio if prio <= EXIT else (EXIT if self.limiter.quotes_suspended() else prio)
        d = await self._req(eff_prio, "GET", "/api/orders/v2", deadline=deadline)
        for o in (d or {}).get("result", []):
            if o.get("rfq_id") == rfq_id:
                return o
        return None

    async def positions(self, deadline: Optional[float] = None) -> dict:
        d = await self._req(EXIT, "GET", "/api/positions", deadline=deadline)
        out = {}
        for p in d if isinstance(d, list) else []:
            info = p.get("position_info", {}) or {}
            asset = (info.get("instrument") or {}).get("underlying")
            if not asset:
                continue
            liq = p.get("estimated_liquidation_price")
            out[asset] = {"qty": float(info.get("qty", 0) or 0),
                          "entry": float(info.get("avg_entry_price", 0) or 0),
                          "liq": float(liq) if liq not in (None, "") else None,
                          "value": float(p.get("value", 0) or 0)}
        return out

    async def portfolio(self) -> dict:
        d = await self._req(EXIT, "GET", "/api/portfolio")
        return {"balance": float(d.get("balance", 0) or 0), "upnl": float(d.get("upnl", 0) or 0)}

    async def set_leverage(self, asset: str, leverage: int) -> None:
        await self._req(EXIT, "POST", "/api/settlement_pools/set_leverage",
                        {"leverage": str(int(leverage)), "asset": asset})

    async def health(self) -> bool:
        try:
            d = await self._req(EXIT, "GET", "/api/me")
        except VarRateLimited:
            # I6 : un 429/418 n'est PAS une perte de session : on renvoie l'état précédent.
            return self._health_ok
        except VarError:
            self._health_ok = False
            return False
        self._health_ok = isinstance(d, dict) and bool(d.get("token"))
        return self._health_ok

    async def supported_assets(self) -> dict:
        return await self._req(EXIT, "GET", "/api/metadata/supported_assets")
