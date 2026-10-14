from __future__ import annotations

import time
from typing import Callable, Optional

from arb.hl_client import DEFAULT_BASE_URL, DEX


class HLFeed:
    def __init__(self, coins: list[str], account: str, on_fill: Callable[[dict], None], loop,
                 base_url: str = DEFAULT_BASE_URL, clock=time.time) -> None:
        self.coins = list(coins)
        self.account = account
        self.on_fill = on_fill
        self.loop = loop
        self.base_url = base_url
        self._clock = clock
        self._books: dict[str, tuple[Optional[float], Optional[float]]] = {}
        # L1 (levers-2026-09-23) : tailles du meilleur niveau (flux bbo), et coins dont le carnet
        # vient du flux bbo (un l2Book, plus lent, ne l'écrase alors jamais).
        self._sizes: dict[str, tuple[Optional[float], Optional[float]]] = {}
        self._bbo_seen: set = set()
        self._last: dict[str, float] = {}
        self._last_fill_msg = clock()
        # amendement t14 (fix) : horodatage du dernier message reçu, tous coins et tous canaux
        # (l2Book ET userFills) confondus. Sert de base au silence GLOBAL, remplaçant l'ancien
        # `stalled()` par coin (trop sensible à la cadence réelle du flux l2Book de ce dex).
        self._last_any = clock()
        self._info = None

    def start(self) -> None:
        from hyperliquid.info import Info
        self._info = Info(base_url=self.base_url, skip_ws=False, perp_dexs=["", DEX])
        # L1 : `bbo` (meilleur bid/ask, poussé 8 à 10 fois par seconde et par coin sur le dex
        # xyz) au lieu de `l2Book` (environ toutes les 5 s sur ce dex : le bot travaillait sur un
        # carnet qui pouvait avoir 5 s de retard). l2Book n'est plus utile : seul le meilleur
        # niveau sert aux décisions.
        for c in self.coins:
            self._info.subscribe({"type": "bbo", "coin": c}, self.handle_bbo)
        self._info.subscribe({"type": "userFills", "user": self.account}, self.handle_fills)

    def stop(self) -> None:
        if self._info is not None:
            try:
                self._info.disconnect_websocket()
            except Exception:
                pass

    def handle_bbo(self, msg: dict) -> None:
        """L1 : message `bbo` {"coin", "time", "bbo": [bid|None, ask|None]}, chaque niveau
        {"px", "sz", "n"}."""
        self._last_any = self._clock()
        d = msg.get("data") or {}
        coin = d.get("coin")
        if coin not in self.coins:
            return
        bb = list(d.get("bbo") or []) + [None, None]
        b, a = bb[0], bb[1]
        try:
            bid = float(b["px"]) if b else None
            ask = float(a["px"]) if a else None
            bsz = float(b.get("sz", 0) or 0) if b else None
            asz = float(a.get("sz", 0) or 0) if a else None
        except (KeyError, TypeError, ValueError):
            return
        self._books[coin] = (bid, ask)
        self._sizes[coin] = (bsz, asz)
        self._bbo_seen.add(coin)
        self._last[coin] = self._clock()

    def handle_book(self, msg: dict) -> None:
        d = msg.get("data") or {}
        coin = d.get("coin")
        self._last_any = self._clock()
        if coin in self._bbo_seen:
            # L1 : le carnet vient du flux bbo, plus frais : un l2Book ne l'écrase jamais.
            return
        levels = d.get("levels") or [[], []]
        bid = float(levels[0][0]["px"]) if levels[0] else None
        ask = float(levels[1][0]["px"]) if len(levels) > 1 and levels[1] else None
        self._books[coin] = (bid, ask)
        self._last[coin] = self._clock()
        self._last_any = self._clock()

    def handle_fills(self, msg: dict) -> None:
        self._last_fill_msg = self._clock()
        self._last_any = self._clock()
        d = msg.get("data") or {}
        if d.get("isSnapshot"):
            return
        for f in d.get("fills", []):
            if f.get("coin") not in self.coins:
                continue
            fill = {"coin": f["coin"], "side": f.get("side"), "px": float(f.get("px", 0)),
                    "sz": float(f.get("sz", 0)), "oid": int(f.get("oid", 0)),
                    "fee": float(f.get("fee", 0) or 0), "tid": int(f.get("tid", 0)),
                    "time": int(f.get("time", 0))}
            self.loop.call_soon_threadsafe(self.on_fill, fill)

    def book(self, coin: str) -> tuple[Optional[float], Optional[float]]:
        return self._books.get(coin, (None, None))

    def book_sizes(self, coin: str) -> tuple[Optional[float], Optional[float]]:
        """L1 : tailles visibles au meilleur bid et au meilleur ask (flux bbo)."""
        return self._sizes.get(coin, (None, None))

    def bbo_age(self, coin: str) -> float:
        """L1 : âge (s) du dernier meilleur prix reçu pour ce coin (alias de book_age)."""
        return self.book_age(coin)

    def mid(self, coin: str) -> Optional[float]:
        b, a = self.book(coin)
        return (b + a) / 2 if b is not None and a is not None else None

    def stalled(self, max_age_s: float) -> bool:
        now = self._clock()
        return any(now - self._last.get(c, -1e18) > max_age_s for c in self.coins)

    def last_any_msg(self) -> float:
        """Horodatage (horloge de ce HLFeed) du dernier message reçu, tous coins et tous canaux
        (l2Book et userFills) confondus. Base du silence GLOBAL (amendement t14 fix)."""
        return self._last_any

    def book_age(self, coin: str) -> float:
        """Âge (s) du dernier carnet reçu pour CE coin. `inf` si aucun n'a jamais été reçu. Sert
        au garde-fou PAR MARCHÉ (carnet HL périmé), indépendant du silence global."""
        last = self._last.get(coin)
        return float("inf") if last is None else max(0.0, self._clock() - last)

    def fills_alive(self, max_age_s: float = 30.0) -> bool:
        # userFills ne pousse rien sans fill : la vivacite se lit sur la connexion du SDK
        ws = getattr(self._info, "ws_manager", None)
        if ws is None:
            return self._info is None
        return bool(getattr(ws, "is_alive", lambda: True)())
