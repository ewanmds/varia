from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Optional

DEFAULT_BASE_URL = "https://api.hyperliquid.xyz"
DEX = "xyz"
HTTP_TIMEOUT_S = 8.0


@dataclass(frozen=True)
class CoinMeta:
    sz_decimals: int
    max_leverage: int
    growth: bool


def parse_xyz_meta(meta: dict) -> dict[str, CoinMeta]:
    out = {}
    for u in meta.get("universe", []):
        out[u["name"]] = CoinMeta(int(u.get("szDecimals", 0)), int(u.get("maxLeverage", 1)),
                                  str(u.get("growthMode", "")).lower() == "enabled")
    return out


def effective_maker_fee_bps(user_add_rate: float, growth: bool) -> float:
    return round(user_add_rate * 1e4 * (0.1 if growth else 1.0) * 2.0, 6)


class HLRefused(RuntimeError):
    """HL a répondu `status: "err"` à une action (refus explicite, pas une panne réseau)."""


# Erreur renvoyée par HL quand l'ordre à annuler n'existe plus : l'annulation a atteint son but.
_CANCEL_GONE = "never placed, already canceled, or filled"


def parse_cancel_status(resp) -> bool:
    """Vrai si l'ordre n'est plus au carnet après cette annulation (I2) : statut global "ok" ET
    statut unitaire "success", ou erreur "ordre déjà annulé/rempli". Tout le reste = échec."""
    if not isinstance(resp, dict) or resp.get("status") != "ok":
        return False
    data = (resp.get("response") or {}).get("data") or {}
    statuses = data.get("statuses")
    if not statuses:
        # réponse sans détail unitaire : le statut global "ok" fait foi.
        return True
    st = statuses[0]
    if st == "success":
        return True
    if isinstance(st, dict) and "error" in st:
        return _CANCEL_GONE in str(st["error"])
    return False


def parse_rate_limit(resp) -> dict:
    """Réponse `userRateLimit` -> marge de requêtes restante (C1d). Champs HL : `cumVlm`,
    `nRequestsUsed`, `nRequestsCap`, `nRequestsSurplus`. Le plafond vaut déjà 10 000 + 1 par
    USDC tradé ; le surplus (requêtes réservées) s'y ajoute."""
    d = resp if isinstance(resp, dict) else {}
    used = int(float(d.get("nRequestsUsed", 0) or 0))
    cap = int(float(d.get("nRequestsCap", 0) or 0))
    surplus = int(float(d.get("nRequestsSurplus", 0) or 0))
    return {"used": used, "cap": cap, "surplus": surplus,
            "cum_vlm": float(d.get("cumVlm", 0) or 0), "headroom": cap + surplus - used}


def parse_order_status(resp: dict) -> dict:
    out = {"oid": None, "filled_sz": 0.0, "avg_px": None, "error": None}
    if not isinstance(resp, dict) or resp.get("status") != "ok":
        out["error"] = str((resp or {}).get("response", resp))
        return out
    statuses = (((resp.get("response") or {}).get("data") or {}).get("statuses") or [{}])
    st = statuses[0] if statuses else {}
    if "resting" in st:
        out["oid"] = int(st["resting"]["oid"])
    elif "filled" in st:
        f = st["filled"]
        out.update(oid=int(f.get("oid", 0)) or None, filled_sz=float(f.get("totalSz", 0)),
                   avg_px=float(f.get("avgPx", 0)) or None)
    elif "error" in st:
        out["error"] = str(st["error"])
    return out


class HLClient:
    def __init__(self, account: str, agent_key: Optional[str], base_url: str = DEFAULT_BASE_URL,
                 paper: bool = False) -> None:
        from hyperliquid.info import Info
        self.account = account
        self.paper = paper
        self.base_url = base_url
        self.info = Info(base_url=base_url, skip_ws=True, perp_dexs=["", DEX], timeout=HTTP_TIMEOUT_S)
        self.exchange = None
        if agent_key and not paper:
            from eth_account import Account
            from hyperliquid.exchange import Exchange
            self.exchange = Exchange(Account.from_key(agent_key), base_url=base_url,
                                     account_address=account, perp_dexs=["", DEX], timeout=HTTP_TIMEOUT_S)
        self.meta: dict[str, CoinMeta] = {}
        self._user_add_rate: Optional[float] = None
        self._user_cross_rate: Optional[float] = None
        # R1 : une seule action d'échange signée à la fois (les nonces HL sont des horodatages en
        # ms : deux actions signées en parallèle depuis deux threads peuvent entrer en collision).
        self._ex_lock = threading.Lock()

    def _post(self, payload: dict):
        return self.info.post("/info", payload)

    def load_meta(self) -> dict[str, CoinMeta]:
        meta, _ctx = self._post({"type": "metaAndAssetCtxs", "dex": DEX})
        self.meta = parse_xyz_meta(meta)
        return self.meta

    def _load_fees(self) -> None:
        if self._user_add_rate is None:
            fees = self._post({"type": "userFees", "user": self.account}) or {}
            self._user_add_rate = float(fees.get("userAddRate", 0.00015))
            self._user_cross_rate = float(fees.get("userCrossRate", 0.00045))

    def maker_fee_bps(self, coin: str) -> float:
        self._load_fees()
        return effective_maker_fee_bps(self._user_add_rate, self.meta.get(coin, CoinMeta(0, 1, False)).growth)

    def taker_fee_bps(self, coin: str) -> float:
        """Frais taker HL estimés (même règle que le maker : growth mode et échelle HIP-3). Sert à
        estimer le PnL réalisé des sorties et corrections (I8)."""
        self._load_fees()
        return effective_maker_fee_bps(self._user_cross_rate, self.meta.get(coin, CoinMeta(0, 1, False)).growth)

    def place_alo(self, coin: str, is_buy: bool, sz: float, px: float) -> dict:
        if self.paper or self.exchange is None:
            return {"oid": None, "filled_sz": 0.0, "avg_px": None, "error": "paper"}
        with self._ex_lock:
            resp = self.exchange.order(coin, is_buy, sz, px, {"limit": {"tif": "Alo"}})
        return parse_order_status(resp)

    def modify_alo(self, coin: str, oid: int, is_buy: bool, sz: float, px: float) -> dict:
        """C1b : déplace un ordre ALO existant en une seule action (au lieu d'annuler puis
        reposer). HL renvoie le nouvel oid au repos, ou une erreur."""
        if self.paper or self.exchange is None:
            return {"oid": None, "filled_sz": 0.0, "avg_px": None, "error": "paper"}
        with self._ex_lock:
            resp = self.exchange.modify_order(int(oid), coin, is_buy, sz, px, {"limit": {"tif": "Alo"}})
        return parse_order_status(resp)

    def ioc(self, coin: str, is_buy: bool, sz: float, px: float, reduce_only: bool = False) -> dict:
        if self.paper or self.exchange is None:
            return {"oid": None, "filled_sz": sz, "avg_px": px, "error": None}
        with self._ex_lock:
            resp = self.exchange.order(coin, is_buy, sz, px, {"limit": {"tif": "Ioc"}}, reduce_only=reduce_only)
        return parse_order_status(resp)

    def cancel(self, coin: str, oid: int) -> bool:
        if self.paper or self.exchange is None:
            return True
        with self._ex_lock:
            resp = self.exchange.cancel(coin, oid)
        return parse_cancel_status(resp)

    def open_orders(self) -> list[dict]:
        return self._post({"type": "frontendOpenOrders", "user": self.account, "dex": DEX}) or []

    def cancel_all(self, coins: list[str]) -> None:
        """Annule tous nos ordres HL sur `coins`, puis relit le carnet (I2) : s'il en reste,
        lève RuntimeError (l'appelant réessaie)."""
        if self.paper or self.exchange is None:
            return
        wanted = set(coins)
        for o in self.open_orders():
            if o.get("coin") in wanted:
                try:
                    with self._ex_lock:
                        self.exchange.cancel(o["coin"], int(o["oid"]))
                except Exception:
                    pass
        left = [o for o in self.open_orders() if o.get("coin") in wanted]
        if left:
            raise RuntimeError(f"{len(left)} ordre(s) HL encore ouvert(s) après annulation globale")

    def schedule_cancel(self, when_ms: Optional[int]) -> bool:
        """I9 : dead-man switch HL. `when_ms` = instant (ms UTC) d'annulation automatique de tous
        les ordres ; None le désarme. Lève RuntimeError si HL refuse (compte non éligible...)."""
        if self.paper or self.exchange is None:
            return True
        with self._ex_lock:
            resp = self.exchange.schedule_cancel(when_ms)
        if not isinstance(resp, dict) or resp.get("status") != "ok":
            raise HLRefused(str((resp or {}).get("response", resp) if isinstance(resp, dict) else resp))
        return True

    def rate_limit(self) -> dict:
        """C1d : limite de débit HL par adresse (requête info, sans coût sur ce budget)."""
        return parse_rate_limit(self._post({"type": "userRateLimit", "user": self.account}))

    def _state(self) -> dict:
        return self._post({"type": "clearinghouseState", "user": self.account, "dex": DEX}) or {}

    def positions_detail(self) -> dict[str, dict]:
        """{coin: {"szi": taille signée, "entry": prix d'entrée moyen ou None}}."""
        out = {}
        for ap in self._state().get("assetPositions", []):
            p = ap.get("position", {})
            entry = float(p.get("entryPx", 0) or 0)
            out[p.get("coin")] = {"szi": float(p.get("szi", 0) or 0), "entry": entry or None}
        return out

    def positions(self) -> dict[str, float]:
        return {c: d["szi"] for c, d in self.positions_detail().items()}

    def margin_ratio(self) -> float:
        st = self._state()
        av = float((st.get("marginSummary") or {}).get("accountValue", 0) or 0)
        used = float(st.get("crossMaintenanceMarginUsed", 0) or 0)
        return used / av if av > 0 else 0.0

    def mids(self) -> dict[str, float]:
        d = self._post({"type": "allMids", "dex": DEX}) or {}
        return {k: float(v) for k, v in d.items()}

    def set_cross_leverage(self, coin: str, lev: int) -> None:
        if self.paper or self.exchange is None:
            return
        with self._ex_lock:
            self.exchange.update_leverage(int(lev), coin, True)
