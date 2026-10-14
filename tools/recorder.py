"""Enregistreur de marché en lecture seule : prix Var (websocket /prices) et carnets + trades HL
(websocket public) pour 5 marchés, horodatés à la milliseconde.

Sortie : data/recordings/<AAAAMMJJ-HHMMSS>/var.jsonl et hl.jsonl
- var.jsonl : {"t": réception locale ms, "ts": horodatage serveur ms (celui du calcul du prix), "a": actif Var, "px": prix}
- hl.jsonl  : {"t", "ts", "c": coin, "k": "q", "bid", "bsz", "ask", "asz"} (meilleur prix, `bbo`,
              poussé à chaque bloc où il change : c'est la série principale)
              {"t", "ts", "c": coin, "k": "b", "b": [[px, sz]...5], "a": [[px, sz]...5]} (carnet
              `l2Book`, 5 niveaux ; sur le dex xyz il n'arrive que toutes les 5 s environ)
              {"t", "ts", "c", "k": "t", "px", "sz", "s": "B"|"A"} (trade)

Aucun ordre, aucune cotation RFQ, aucun appel /exchange : uniquement des abonnements websocket.
Usage : python3 tools/recorder.py --minutes 120
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from arb.var_ws import VarPriceStream  # noqa: E402

HL_WS_URL = "wss://api.hyperliquid.xyz/ws"
MARKETS = [  # (actif Var, kind Var, coin HL)
    ("US100S", "index", "xyz:XYZ100"),
    ("US500S", "index", "xyz:SP500"),
    ("XAUS", "commodity", "xyz:GOLD"),
    ("USOILP", "commodity", "xyz:CL"),
    ("UKOILP", "commodity", "xyz:BRENTOIL"),
]
_HL_ALLOWED = {"subscribe", "ping"}


def now_ms() -> int:
    return int(time.time() * 1000)


class JsonlWriter:
    def __init__(self, path: Path) -> None:
        self.f = open(path, "a", buffering=1 << 16)
        self.count = 0
        self.by_key: dict[str, int] = {}

    def write(self, rec: dict, key: str) -> None:
        self.f.write(json.dumps(rec, separators=(",", ":")) + "\n")
        self.count += 1
        self.by_key[key] = self.by_key.get(key, 0) + 1

    def flush(self) -> None:
        self.f.flush()

    def close(self) -> None:
        self.f.close()


def hl_records(msg: dict, recv_ms: int) -> list[tuple[dict, str]]:
    ch = msg.get("channel")
    d = msg.get("data")
    out = []
    if ch == "l2Book" and isinstance(d, dict):
        lv = d.get("levels") or [[], []]
        side = lambda xs: [[float(x["px"]), float(x["sz"])] for x in (xs or [])[:5]]  # noqa: E731
        out.append(({"t": recv_ms, "ts": d.get("time"), "c": d.get("coin"), "k": "b",
                     "b": side(lv[0]), "a": side(lv[1] if len(lv) > 1 else [])}, d.get("coin")))
    elif ch == "bbo" and isinstance(d, dict):
        bb = d.get("bbo") or [None, None]
        b, a = (bb + [None, None])[:2]
        out.append(({"t": recv_ms, "ts": d.get("time"), "c": d.get("coin"), "k": "q",
                     "bid": float(b["px"]) if b else None, "bsz": float(b["sz"]) if b else None,
                     "ask": float(a["px"]) if a else None, "asz": float(a["sz"]) if a else None},
                    str(d.get("coin")) + ":bbo"))
    elif ch == "trades" and isinstance(d, list):
        for tr in d:
            out.append(({"t": recv_ms, "ts": tr.get("time"), "c": tr.get("coin"), "k": "t",
                         "px": float(tr["px"]), "sz": float(tr["sz"]), "s": tr.get("side")},
                        str(tr.get("coin")) + ":trades"))
    return out


async def hl_loop(coins: list[str], writer: JsonlWriter, stop: asyncio.Event, trades: bool) -> None:
    import websockets

    async def send(ws, msg):
        assert msg.get("method") in _HL_ALLOWED, msg
        await ws.send(json.dumps(msg))

    while not stop.is_set():
        try:
            async with websockets.connect(HL_WS_URL, open_timeout=15, ping_interval=None,
                                          max_size=2 ** 22) as ws:
                for c in coins:
                    await send(ws, {"method": "subscribe", "subscription": {"type": "bbo", "coin": c}})
                    await send(ws, {"method": "subscribe", "subscription": {"type": "l2Book", "coin": c}})
                    if trades:
                        await send(ws, {"method": "subscribe", "subscription": {"type": "trades", "coin": c}})
                last_ping = time.monotonic()
                while not stop.is_set():
                    if time.monotonic() - last_ping > 30:
                        await send(ws, {"method": "ping"})
                        last_ping = time.monotonic()
                    try:
                        raw = await asyncio.wait_for(ws.recv(), 5)
                    except asyncio.TimeoutError:
                        continue
                    t = now_ms()
                    try:
                        msg = json.loads(raw)
                    except ValueError:
                        continue
                    for rec, key in hl_records(msg, t):
                        writer.write(rec, key)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[hl] connexion perdue : {type(e).__name__}: {e}", flush=True)
            try:
                await asyncio.wait_for(stop.wait(), 2)
            except asyncio.TimeoutError:
                pass


async def main_async(args) -> None:
    out = Path(args.out) if args.out else ROOT / "data" / "recordings" / datetime.now().strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    var_w = JsonlWriter(out / "var.jsonl")
    hl_w = JsonlWriter(out / "hl.jsonl")
    (out / "meta.json").write_text(json.dumps({"start_ms": now_ms(), "minutes": args.minutes,
                                               "markets": MARKETS}, indent=1))
    print(f"[rec] enregistrement dans {out} pour {args.minutes} min", flush=True)

    def on_var(asset, u):
        var_w.write({"t": u["ts_recv_ms"], "ts": u["ts_server_ms"], "a": asset, "px": u["mark"]}, asset)

    stop = asyncio.Event()
    var = VarPriceStream({a: k for a, k, _ in MARKETS}, on_update=on_var)
    tasks = [asyncio.create_task(var.run(stop)),
             asyncio.create_task(hl_loop([c for _, _, c in MARKETS], hl_w, stop, not args.no_trades))]
    end = time.monotonic() + args.minutes * 60
    prev_v, prev_h = 0, 0
    try:
        while time.monotonic() < end:
            await asyncio.sleep(min(60, max(0.1, end - time.monotonic())))
            var_w.flush()
            hl_w.flush()
            print(f"[rec] {datetime.now():%H:%M:%S} var +{var_w.count - prev_v} (total {var_w.count}) "
                  f"{dict(sorted(var_w.by_key.items()))} | hl +{hl_w.count - prev_h} (total {hl_w.count}) "
                  f"{dict(sorted(hl_w.by_key.items()))}", flush=True)
            prev_v, prev_h = var_w.count, hl_w.count
    finally:
        stop.set()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        var_w.close()
        hl_w.close()
        print(f"[rec] terminé : var {var_w.count} lignes, hl {hl_w.count} lignes -> {out}", flush=True)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--minutes", type=float, default=120.0)
    p.add_argument("--out", default=None, help="dossier de sortie (défaut data/recordings/<date-heure>)")
    p.add_argument("--no-trades", action="store_true", help="ne pas s'abonner aux trades HL")
    args = p.parse_args()
    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
