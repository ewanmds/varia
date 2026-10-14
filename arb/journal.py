from __future__ import annotations

import csv
import sqlite3
import sys
import threading
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS arbs (id INTEGER PRIMARY KEY, ts REAL, market TEXT, hl_side TEXT, hl_px REAL,
  hl_sz REAL, hl_fee REAL, var_side TEXT, var_px REAL, var_qty REAL, edge_bps REAL, pnl_usd REAL,
  latency_ms REAL, status TEXT);
CREATE TABLE IF NOT EXISTS incidents (id INTEGER PRIMARY KEY, ts REAL, market TEXT, level TEXT, msg TEXT);
CREATE TABLE IF NOT EXISTS strikes (id INTEGER PRIMARY KEY, ts REAL);
"""


def _day_bounds(day: date) -> tuple[float, float]:
    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp()
    return start, start + 86400


class Journal:
    def __init__(self, path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # C2 : la connexion est partagée entre la boucle asyncio et des threads (serveur, to_thread) :
        # toutes les opérations SQLite passent par ce verrou.
        self._lock = threading.RLock()
        self.db = sqlite3.connect(str(self.path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        with self._lock:
            self.db.executescript(_SCHEMA)

    def record_arb(self, market, hl_side, hl_px, hl_sz, hl_fee, var_side, var_px, var_qty,
                   edge_bps, pnl_usd, latency_ms, status) -> int:
        with self._lock:
            cur = self.db.execute(
                "INSERT INTO arbs (ts, market, hl_side, hl_px, hl_sz, hl_fee, var_side, var_px, var_qty,"
                " edge_bps, pnl_usd, latency_ms, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (time.time(), market, hl_side, hl_px, hl_sz, hl_fee, var_side, var_px, var_qty,
                 edge_bps, pnl_usd, latency_ms, status))
            self.db.commit()
            return int(cur.lastrowid)

    def record_incident(self, market: str, level: str, msg: str) -> None:
        with self._lock:
            self.db.execute("INSERT INTO incidents (ts, market, level, msg) VALUES (?,?,?,?)",
                            (time.time(), market, level, msg))
            self.db.commit()

    def record_strike(self, ts: Optional[float] = None) -> None:
        """I6 : persiste un strike Var (429/418) pour le recharger après un redémarrage."""
        with self._lock:
            self.db.execute("INSERT INTO strikes (ts) VALUES (?)", (time.time() if ts is None else float(ts),))
            self.db.commit()

    def strikes_since(self, ts: float) -> list[float]:
        with self._lock:
            return [float(r[0]) for r in self.db.execute("SELECT ts FROM strikes WHERE ts>=? ORDER BY ts", (ts,))]

    def daily_pnl(self, day: Optional[date] = None) -> float:
        day = day or datetime.now(timezone.utc).date()
        a, b = _day_bounds(day)
        with self._lock:
            row = self.db.execute("SELECT COALESCE(SUM(pnl_usd),0) FROM arbs WHERE ts>=? AND ts<?", (a, b)).fetchone()
        return float(row[0])

    def pnl_since(self, ts: float) -> tuple[float, int]:
        """PnL cumulé et nombre de lignes du journal depuis l'instant ts (début de session)."""
        with self._lock:
            row = self.db.execute("SELECT COALESCE(SUM(pnl_usd),0), COUNT(*) FROM arbs WHERE ts>=?", (ts,)).fetchone()
        return float(row[0]), int(row[1])

    def volume_since(self, ts: float, status: str) -> float:
        """Volume en $ (prix x quantité Var) des lignes d'un statut depuis l'instant ts."""
        with self._lock:
            row = self.db.execute("SELECT COALESCE(SUM(var_px*var_qty),0) FROM arbs WHERE ts>=? AND status=?",
                                  (ts, status)).fetchone()
        return float(row[0])

    def recent_arbs(self, n: int = 50) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self.db.execute("SELECT * FROM arbs ORDER BY id DESC LIMIT ?", (n,))]

    def recent_incidents(self, n: int = 50) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self.db.execute("SELECT * FROM incidents ORDER BY id DESC LIMIT ?", (n,))]

    def export_csv(self, day: date, out_dir: Path) -> Path:
        a, b = _day_bounds(day)
        with self._lock:
            rows = [dict(r) for r in self.db.execute("SELECT * FROM arbs WHERE ts>=? AND ts<? ORDER BY id", (a, b))]
        out = Path(out_dir) / f"arbs-{day.isoformat()}.csv"
        cols = ["id", "ts", "market", "hl_side", "hl_px", "hl_sz", "hl_fee", "var_side", "var_px",
                "var_qty", "edge_bps", "pnl_usd", "latency_ms", "status"]
        with out.open("w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=cols)
            w.writeheader()
            for r in rows:
                w.writerow({c: r.get(c) for c in cols})
        return out


class SafeJournal:
    """C2 : enveloppe d'un journal dont les ÉCRITURES ne lèvent jamais. Un journal qui échoue
    (disque plein, base verrouillée...) ne doit jamais interrompre une couverture ou une sortie :
    l'échec part en log console et le flux de trading continue. Les lectures et les autres
    attributs sont délégués tels quels au journal enveloppé."""

    _WRITES = ("record_arb", "record_incident", "record_strike")

    def __init__(self, inner) -> None:
        self._inner = inner

    @staticmethod
    def wrap(j):
        return j if isinstance(j, SafeJournal) else SafeJournal(j)

    def _safe(self, name: str, *args):
        try:
            return getattr(self._inner, name)(*args)
        except Exception as exc:
            print(f"[journal] ÉCHEC d'écriture {name}{args!r} : {type(exc).__name__}: {exc}",
                  file=sys.stderr, flush=True)
            return None

    def record_arb(self, *args):
        return self._safe("record_arb", *args)

    def record_incident(self, *args):
        return self._safe("record_incident", *args)

    def record_strike(self, *args):
        return self._safe("record_strike", *args)

    def __getattr__(self, name):
        return getattr(self._inner, name)
