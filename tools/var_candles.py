"""Télécharge l'historique des bougies Variational (API publique du front /api/candles) pour un ou
plusieurs actifs, en lecture seule, via le Chrome dédié (Cloudflare bloque les appels directs).

Usage : python3 tools/var_candles.py [--port 9223] [--period 1m] [--since 2026-09-08] USOILP UKOILP
Résultat : data/candles/<ACTIF>_<period>.csv (unix_time_ms, open, high, low, close), complété à
chaque lancement (les bougies déjà présentes ne sont pas retéléchargées).
"""
from __future__ import annotations

import argparse
import asyncio
import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "data" / "candles"
PERIOD_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000}
CHUNK_BARS = 1000          # taille de tranche utilisée par le front
PAUSE_S = 1.2              # espacement des requêtes (lecture seule, rythme modéré)

_FETCH_JS = """
async (url) => {
  const r = await fetch(url, {credentials: 'include'});
  let j = null; try { j = await r.json(); } catch (e) {}
  return [r.status, j];
}
"""


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _load(path: Path) -> dict[int, list]:
    rows: dict[int, list] = {}
    if path.exists():
        with path.open() as fh:
            for r in csv.DictReader(fh):
                rows[int(r["unix_time_ms"])] = [float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"])]
    return rows


def _save(path: Path, rows: dict[int, list]) -> None:
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["unix_time_ms", "open", "high", "low", "close"])
        for t in sorted(rows):
            w.writerow([t, *rows[t]])
    tmp.replace(path)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("assets", nargs="+")
    ap.add_argument("--port", type=int, default=9223)
    ap.add_argument("--period", default="1m", choices=sorted(PERIOD_MS))
    ap.add_argument("--since", default="2026-09-08")
    args = ap.parse_args()
    from playwright.async_api import async_playwright

    OUT.mkdir(parents=True, exist_ok=True)
    step = PERIOD_MS[args.period] * CHUNK_BARS
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    since_ms = int(datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc).timestamp() * 1000)
    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(f"http://127.0.0.1:{args.port}")
        page = next(p for c in browser.contexts for p in c.pages if "variational.io" in p.url)
        for asset in args.assets:
            path = OUT / f"{asset}_{args.period}.csv"
            rows = _load(path)
            start = max(since_ms, (max(rows) + PERIOD_MS[args.period]) if rows else since_ms)
            n_req = 0
            while start < now_ms:
                end = min(start + step, now_ms)
                url = f"/api/candles?period={args.period}&start={_iso(start)}&end={_iso(end)}&cex_asset={asset}"
                status, data = await page.evaluate(_FETCH_JS, url)
                n_req += 1
                if status != 200:
                    print(f"{asset} {_iso(start)} : HTTP {status}, arrêt de cet actif", flush=True)
                    break
                for c in data or []:
                    rows[int(c["unix_time_ms"])] = [c["open"], c["high"], c["low"], c["close"]]
                start = end
                await asyncio.sleep(PAUSE_S)
            _save(path, rows)
            first = _iso(min(rows)) if rows else "-"
            print(f"{asset} : {len(rows)} bougies {args.period} depuis {first} ({n_req} requêtes) -> {path}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
