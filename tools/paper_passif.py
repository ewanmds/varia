"""Paper passif v2 : simule le mode chase à plusieurs seuils (par défaut 1, 3 et 5 pb) à partir
des prix publiés par le bot réel (GET /api/status en local), sans AUCUNE requête Var ni HL.

Modèle, par marché et par seuil, avec UNE position de fill_usd au plus (comme le bot réel) :
- à plat : chase d'entrée des deux côtés. On vend sur HL si HL est au-dessus de Var, on achète
  s'il est en dessous. Le seuil total est seuil + frais maker estimés + réserve de glissement,
  mesuré contre le bid/ask Var corrigé du basis. L'ordre simulé suit le best HL (au plus 1
  déplacement par seconde) et n'est jamais placé au-delà du plancher. Il est annulé si l'écart
  retombe, si la cotation Var a plus de 5 s, ou après 30 s de vie, avec ensuite 3 s d'attente ;
- fill simulé PRUDENT : seulement si le prix HL traverse notre niveau. La file d'attente et les
  fills « au niveau » sont ignorés, donc le nombre de fills est une borne basse. La couverture Var
  se fait au bid/ask Var du moment ;
- en position : chase de SORTIE du côté qui réduit, au seuil exit_edge (0 pb + frais + réserve),
  comme le bot. Si rien n'a été rempli avant la fermeture (21:00 UTC - 10 min), sortie taker au
  best HL et au bid/ask Var, avec les frais taker HL ;
- PnL réalisé par cycle = jambe HL + jambe Var calculées sur les prix, moins les frais HL.

Usage : python3 tools/paper_passif.py [seuils...]   Ctrl+C pour arrêter (un bilan final est écrit).
Résultats : data/paper-passif/summary.json (toutes les 30 s) et events.jsonl.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

STATUS_URL = "http://127.0.0.1:8791/api/status"
OUT = Path(__file__).resolve().parents[1] / "data" / "paper-passif"
SLIP_BPS = 2.0
EXIT_EDGE_BPS = 0.0
MAKER_FEE_BPS = {"xyz:GOLD": 3.0}   # growth mode : environ 0,3 pb ; GOLD hors growth mode : 3 pb
TAKER_FEE_BPS = {"xyz:GOLD": 9.0}   # growth mode : environ 0,9 pb ; GOLD : 9 pb
DEFAULT_MAKER, DEFAULT_TAKER = 0.3, 0.9
TTL_S, COOLDOWN_S, MOVE_S, MAX_QUOTE_AGE_S = 30.0, 3.0, 1.0, 5.0
FILL_USD = 500.0
CLOSE_UTC_H, TAKER_EXIT_MIN = 21, 10


def fee(table, coin, default):
    return table.get(coin, default)


class Book:
    """Une position simulée (0 ou 1 lot) et au plus un ordre chase actif."""

    def __init__(self) -> None:
        self.pos = 0            # +1 = long HL / short Var ; -1 = short HL / long Var
        self.qty = 0.0
        self.entry_hl = self.entry_var = 0.0
        self.order = None       # {"side", "px", "born", "moved"}
        self.cooldown_until = 0.0
        self.in_opp = False
        self.s = {"detections": 0, "entries": 0, "exits_maker": 0, "exits_taker": 0,
                  "orders": 0, "moves": 0, "cancels": 0, "pnl_usd": 0.0, "entry_edge_sum_bps": 0.0}


def log_event(ev: dict) -> None:
    with (OUT / "events.jsonl").open("a") as fh:
        fh.write(json.dumps(ev, ensure_ascii=False) + "\n")


def close_cycle(bk: Book, m: dict, thr: float, hl_px: float, var_px: float, how: str, now: float) -> None:
    coin = m["coin"]
    exit_fee = fee(TAKER_FEE_BPS, coin, DEFAULT_TAKER) if how == "taker" else fee(MAKER_FEE_BPS, coin, DEFAULT_MAKER)
    entry_fee = fee(MAKER_FEE_BPS, coin, DEFAULT_MAKER)
    if bk.pos < 0:   # short HL / long Var : on rachète HL, on revend Var
        gross = (bk.entry_hl - hl_px) + (var_px - bk.entry_var)
    else:            # long HL / short Var
        gross = (hl_px - bk.entry_hl) + (bk.entry_var - var_px)
    fees = (entry_fee * bk.entry_hl + exit_fee * hl_px) / 1e4
    pnl = (gross - fees) * bk.qty
    bk.s["pnl_usd"] += pnl
    bk.s["exits_maker" if how == "maker" else "exits_taker"] += 1
    log_event({"t": now, "asset": m["asset"], "thr": thr, "event": f"exit_{how}", "pnl_usd": round(pnl, 4)})
    bk.pos, bk.qty, bk.order = 0, 0.0, None
    bk.cooldown_until = now + COOLDOWN_S


def step(bk: Book, m: dict, thr: float, now: float) -> None:
    hb, ha, vb, va = m.get("hl_bid"), m.get("hl_ask"), m.get("var_bid"), m.get("var_ask")
    if None in (hb, ha, vb, va):
        return
    coin = m["coin"]
    basis = m.get("basis") or 0.0
    fresh = m.get("var_age") is not None and m["var_age"] <= MAX_QUOTE_AGE_S
    mk = fee(MAKER_FEE_BPS, coin, DEFAULT_MAKER)
    utc = datetime.now(timezone.utc)
    minutes_to_close = (CLOSE_UTC_H * 60) - (utc.hour * 60 + utc.minute)

    # sortie taker avant la fermeture
    if bk.pos != 0 and 0 <= minutes_to_close <= TAKER_EXIT_MIN:
        if bk.pos < 0:
            close_cycle(bk, m, thr, ha, vb, "taker", now)   # rachat HL au ask, vente Var au bid
        else:
            close_cycle(bk, m, thr, hb, va, "taker", now)
        return
    if 0 <= minutes_to_close <= 20:
        bk.order = None   # plus de nouvelles entrées à l'approche de la fermeture
        if bk.pos == 0:
            return

    # côtés possibles : à plat, entrée des deux côtés ; en position, seulement le côté qui réduit
    if bk.pos == 0:
        candidates = [("sell", thr), ("buy", thr)]
    else:
        candidates = [("buy" if bk.pos < 0 else "sell", EXIT_EDGE_BPS)]
    opp_now = False
    chosen = None
    for side, t in candidates:
        total = (t + mk + SLIP_BPS) / 1e4
        if side == "sell":
            ref, best = va + basis, ha
            ok = fresh and best >= ref * (1 + total)
        else:
            ref, best = vb + basis, hb
            ok = fresh and best <= ref * (1 - total)
        if ok:
            opp_now = True
            chosen = chosen or (side, best, ref)
    if bk.pos == 0 and opp_now and not bk.in_opp:
        bk.s["detections"] += 1
    bk.in_opp = opp_now

    o = bk.order
    if o is not None:
        crossed = (hb >= o["px"]) if o["side"] == "sell" else (ha <= o["px"])
        if crossed:
            var_px = va if o["side"] == "sell" else vb   # couverture Var au bid/ask du moment
            if bk.pos == 0:
                bk.pos = -1 if o["side"] == "sell" else 1
                bk.qty = FILL_USD / o["px"]
                bk.entry_hl, bk.entry_var = o["px"], var_px
                edge = ((o["px"] - (var_px + basis)) if o["side"] == "sell" else ((var_px + basis) - o["px"])) / o["px"] * 1e4
                bk.s["entries"] += 1
                bk.s["entry_edge_sum_bps"] += edge
                log_event({"t": now, "asset": m["asset"], "thr": thr, "event": "entry", "side": o["side"],
                           "hl": o["px"], "var": var_px, "edge_bps": round(edge, 2)})
                bk.order = None
                bk.cooldown_until = now + COOLDOWN_S
            else:
                close_cycle(bk, m, thr, o["px"], var_px, "maker", now)
            return
        still = chosen is not None and chosen[0] == o["side"]
        if not still or now - o["born"] > TTL_S:
            bk.s["cancels"] += 1
            bk.order = None
            bk.cooldown_until = now + COOLDOWN_S
            return
        if chosen[1] != o["px"] and now - o["moved"] >= MOVE_S:
            o["px"], o["moved"] = chosen[1], now
            bk.s["moves"] += 1
        return
    if chosen is not None and now >= bk.cooldown_until and not (bk.pos == 0 and 0 <= minutes_to_close <= 20):
        bk.order = {"side": chosen[0], "px": chosen[1], "born": now, "moved": now}
        bk.s["orders"] += 1


def summarize(books: dict, thresholds, t0: float, now: float) -> dict:
    summary = {"since": time.strftime("%H:%M:%S", time.localtime(t0)), "elapsed_min": round((now - t0) / 60, 1),
               "thresholds": {}}
    for thr in thresholds:
        agg = {"detections": 0, "entries": 0, "exits_maker": 0, "exits_taker": 0, "open_positions": 0,
               "pnl_usd": 0.0, "hl_requests_est": 0, "by_market": {}}
        for (asset, t), bk in books.items():
            if t != thr:
                continue
            s = bk.s
            for k in ("detections", "entries", "exits_maker", "exits_taker"):
                agg[k] += s[k]
            agg["open_positions"] += 1 if bk.pos else 0
            agg["pnl_usd"] += s["pnl_usd"]
            agg["hl_requests_est"] += s["orders"] + s["moves"] + s["cancels"]
            agg["by_market"][asset] = {
                "detections": s["detections"], "entries": s["entries"], "exits_maker": s["exits_maker"],
                "exits_taker": s["exits_taker"], "pnl_usd": round(s["pnl_usd"], 3), "open": bk.pos,
                "avg_entry_edge_bps": round(s["entry_edge_sum_bps"] / s["entries"], 2) if s["entries"] else None}
        agg["pnl_usd"] = round(agg["pnl_usd"], 2)
        summary["thresholds"][f"{thr:g}pb"] = agg
    return summary


def main() -> None:
    thresholds = [float(x) for x in sys.argv[1:]] or [1.0, 3.0, 5.0]
    OUT.mkdir(parents=True, exist_ok=True)
    books: dict = {}
    t0 = time.time()
    last_dump = 0.0
    print(f"Paper passif v2, seuils {thresholds} pb, 1 position de {FILL_USD:g} $ par marché. Résultats dans {OUT}", flush=True)
    try:
        while True:
            now = time.time()
            try:
                with urllib.request.urlopen(STATUS_URL, timeout=3) as r:
                    d = json.load(r)
            except Exception as exc:
                print(f"statut indisponible : {exc}", flush=True)
                time.sleep(2)
                continue
            if d.get("running"):
                for m in d.get("markets", []):
                    for thr in thresholds:
                        step(books.setdefault((m["asset"], thr), Book()), m, thr, now)
            if now - last_dump >= 30:
                last_dump = now
                summary = summarize(books, thresholds, t0, now)
                (OUT / "summary.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False))
                print(f"[{time.strftime('%H:%M:%S')}] " + " | ".join(
                    f"{k}: {v['entries']} entrées, {v['exits_maker'] + v['exits_taker']} sorties, {v['pnl_usd']} $"
                    for k, v in summary["thresholds"].items()), flush=True)
            time.sleep(1.0)
    except KeyboardInterrupt:
        summary = summarize(books, thresholds, t0, time.time())
        (OUT / "summary.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
