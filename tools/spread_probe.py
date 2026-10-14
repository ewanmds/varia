"""Mesure du spread Variational sur les swaps pétrole, par cotations RFQ espacées (aucun ordre).

Toutes les --every secondes, pour chaque actif et chaque taille (en $), demande une cotation
indicative et enregistre bid, ask, mark et spread en pb. Les requêtes sont espacées de 2 s.
Avec les valeurs par défaut (2 actifs x 2 tailles toutes les 120 s), cela fait 2 requêtes par
minute, très loin de toute limite.

Usage : python3 tools/spread_probe.py [--port 9223] [--every 120] [--sizes 2500 5000] [--hours 48]
Résultat : data/spreads/spreads.csv (une ligne par cotation).
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import time
from datetime import datetime, timezone
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "data" / "spreads"
ASSETS = {"USOILP": "commodity", "UKOILP": "commodity"}

_QUOTE_JS = """
async ([asset, kind, qty]) => {
  const inst = {instrument_type: 'swap', underlying: asset, settlement_asset: 'USDC', kind, funding_interval_s: 0};
  const r = await fetch('/api/quotes/indicative', {method: 'POST', credentials: 'include',
    headers: {'Content-Type': 'application/json'}, body: JSON.stringify({instrument: inst, qty})});
  let j = null; try { j = await r.json(); } catch (e) {}
  return [r.status, j];
}
"""


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9223)
    ap.add_argument("--every", type=float, default=120.0)
    ap.add_argument("--sizes", type=float, nargs="+", default=[2500.0, 5000.0])
    ap.add_argument("--hours", type=float, default=48.0)
    args = ap.parse_args()
    from playwright.async_api import async_playwright

    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / "spreads.csv"
    new = not path.exists()
    ref: dict[str, float] = {}
    deadline = time.time() + args.hours * 3600
    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(f"http://127.0.0.1:{args.port}")
        page = next(p for c in browser.contexts for p in c.pages if "variational.io" in p.url)
        with path.open("a", newline="") as fh:
            w = csv.writer(fh)
            if new:
                w.writerow(["ts_utc", "asset", "size_usd", "qty", "bid", "ask", "mark", "spread_bps", "status"])
            while time.time() < deadline:
                cycle = time.time()
                for asset, kind in ASSETS.items():
                    for usd in args.sizes:
                        px = ref.get(asset, 100.0)
                        qty = f"{usd / px:.3f}"
                        try:
                            status, j = await page.evaluate(_QUOTE_JS, [asset, kind, qty])
                        except Exception as exc:
                            status, j = -1, {"error": str(exc)[:100]}
                        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
                        if status == 200 and j:
                            bid, ask = float(j["bid"]), float(j["ask"])
                            mid = (bid + ask) / 2
                            ref[asset] = mid
                            w.writerow([ts, asset, usd, qty, bid, ask, j.get("mark_price"),
                                        round((ask - bid) / mid * 1e4, 3), status])
                        else:
                            w.writerow([ts, asset, usd, qty, "", "", "", "", status])
                            if status in (418, 429):
                                print(f"{ts} : {status} reçu, pause de 10 min", flush=True)
                                await asyncio.sleep(600)
                        fh.flush()
                        await asyncio.sleep(2.0)
                await asyncio.sleep(max(0.0, args.every - (time.time() - cycle)))


if __name__ == "__main__":
    asyncio.run(main())
