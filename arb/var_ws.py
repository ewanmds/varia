"""Flux de prix Var par websocket (lecture seule).

Trouvé dans le bundle du front Omni (omni-v3.10.4, 23/09/2026) :
- URL : `wss://omni-ws-server.prod.ap-northeast-1.variational.io/prices` (connexion
  « marketData » du front). Le flux est public : aucune authentification, aucun cookie.
- Abonnement : `{"action":"subscribe","instruments":[<instrument>]}` où `<instrument>` est le
  même objet que pour les cotations (`swap_instrument`). Désabonnement : `"unsubscribe"`.
- Messages : `{"channel":"instrument_price:C-CMD-USOILP-USDC-0","pricing":{"price":"91.06",
  "underlying_price":"91.06","timestamp":"2026-09-23T14:54:39.875273Z",...}}` environ 1 fois par
  seconde et par instrument, plus des `{"type":"heartbeat"}` toutes les 5 s.
- Le flux ne porte PAS de bid/ask : seulement un prix unique (`price`, égal à
  `underlying_price` sur les swaps), traité ici comme le mark. `bid`/`ask` valent None.

Ce client n'envoie jamais rien d'autre que subscribe, unsubscribe ou ping (garde `_send`).
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from typing import Callable, Optional

from arb.var_client import swap_instrument

VAR_PRICES_URL = "wss://omni-ws-server.prod.ap-northeast-1.variational.io/prices"
CHANNEL_PREFIX = "instrument_price:"
_KIND_CODE = {"equity": "EQY", "commodity": "CMD", "index": "IDX", "etf": "ETF", "currency": "FX"}
_ALLOWED_ACTIONS = {"subscribe", "unsubscribe", "ping"}


def channel_key(asset: str, kind: str) -> str:
    """Clé de canal du front (fonction `Re` du bundle) : C-<CLASSE>-<ACTIF>-USDC-<funding_s>."""
    return f"C-{_KIND_CODE[kind]}-{asset}-USDC-0"


def parse_ts_ms(s) -> Optional[int]:
    """ISO 8601 avec nanosecondes ('2026-09-23T14:54:39.256963825Z') -> ms epoch."""
    if not isinstance(s, str) or not s:
        return None
    try:
        s = s.rstrip("Z")
        frac = ""
        if "." in s:
            s, frac = s.split(".", 1)
            frac = "".join(ch for ch in frac if ch.isdigit())
        base = datetime.strptime(s, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        ms = int(base.timestamp()) * 1000
        if frac:
            ms += int((frac + "000")[:3])
        return ms
    except ValueError:
        return None


class ForbiddenSend(RuntimeError):
    pass


class VarPriceStream:
    """Abonné au flux `/prices` pour quelques swaps. `assets` : {actif: kind} (kind du config)."""

    def __init__(self, assets: dict[str, str], on_update: Optional[Callable[[str, dict], None]] = None,
                 url: str = VAR_PRICES_URL, connect=None,
                 clock_ms: Callable[[], int] = lambda: int(time.time() * 1000),
                 reconnect_s: float = 2.0) -> None:
        self.assets = dict(assets)
        self.on_update = on_update
        self.url = url
        self._connect = connect
        self._clock_ms = clock_ms
        self.reconnect_s = reconnect_s
        self._by_channel = {CHANNEL_PREFIX + channel_key(a, k): a for a, k in self.assets.items()}
        self._latest: dict[str, dict] = {}
        self._ws = None
        self.connects = 0
        self.last_msg_ms: Optional[int] = None

    # --- envoi : seule porte de sortie vers le serveur ---
    async def _send(self, msg: dict) -> None:
        if msg.get("action") not in _ALLOWED_ACTIONS or set(msg) - {"action", "instruments"}:
            raise ForbiddenSend(f"message interdit : {msg!r}")
        await self._ws.send(json.dumps(msg))

    def subscribe_message(self, action: str = "subscribe") -> dict:
        return {"action": action,
                "instruments": [swap_instrument(a, k) for a, k in self.assets.items()]}

    async def subscribe(self) -> None:
        await self._send(self.subscribe_message("subscribe"))

    async def unsubscribe(self) -> None:
        await self._send(self.subscribe_message("unsubscribe"))

    # --- réception ---
    def handle_raw(self, raw, recv_ms: Optional[int] = None) -> Optional[tuple[str, dict]]:
        recv_ms = self._clock_ms() if recv_ms is None else recv_ms
        self.last_msg_ms = recv_ms
        try:
            msg = json.loads(raw)
        except (TypeError, ValueError):
            return None
        if not isinstance(msg, dict):
            return None
        asset = self._by_channel.get(msg.get("channel"))
        pricing = msg.get("pricing")
        if asset is None or not isinstance(pricing, dict):
            return None
        try:
            mark = float(pricing["price"])
        except (KeyError, TypeError, ValueError):
            return None
        if mark <= 0:
            return None
        und = pricing.get("underlying_price")
        upd = {"bid": None, "ask": None, "mark": mark,
               "underlying": float(und) if und not in (None, "") else None,
               "ts_recv_ms": recv_ms, "ts_server_ms": parse_ts_ms(pricing.get("timestamp"))}
        self._latest[asset] = upd
        if self.on_update is not None:
            self.on_update(asset, upd)
        return asset, upd

    def latest(self, asset: str) -> Optional[dict]:
        return self._latest.get(asset)

    def force_reconnect(self) -> None:
        """Ferme la connexion courante (flux muet) : `run()` se reconnecte ensuite."""
        ws = self._ws
        if ws is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._close_task = loop.create_task(ws.close())

    async def _open(self):
        if self._connect is not None:
            return await self._connect(self.url)
        import websockets
        return await websockets.connect(self.url, open_timeout=15, ping_interval=20,
                                        ping_timeout=20, max_size=2 ** 20)

    async def run_once(self) -> None:
        """Une connexion : ouvre, s'abonne, lit jusqu'à la fermeture."""
        self._ws = await self._open()
        self.connects += 1
        try:
            await self.subscribe()
            async for raw in self._ws:
                self.handle_raw(raw)
        finally:
            try:
                await self._ws.close()
            except Exception:
                pass

    async def run(self, stop: Optional[asyncio.Event] = None) -> None:
        """Boucle avec reconnexion jusqu'à `stop`."""
        stop = stop or asyncio.Event()
        while not stop.is_set():
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # coupure réseau : on se reconnecte
                print(f"[var_ws] connexion perdue : {type(e).__name__}: {e}", flush=True)
            if stop.is_set():
                break
            try:
                await asyncio.wait_for(stop.wait(), self.reconnect_s)
            except asyncio.TimeoutError:
                pass
