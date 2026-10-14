from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from arb.config import GlobalCfg

OPEN, WIDE, CLOSING, CLOSING_TAKER, FLAT, CLOSED = "open", "wide", "closing", "closing_taker", "flat", "closed"


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def parse_sessions(asset_entry: dict) -> list[tuple[datetime, datetime]]:
    out = []
    for s in asset_entry.get("trading_sessions") or []:
        try:
            out.append((_dt(s["open"]), _dt(s["close"])))
        except (KeyError, ValueError):
            continue
    return sorted(out)


def phase_at(now: datetime, sessions, g: GlobalCfg) -> str:
    for op, cl in sessions:
        if not (op <= now < cl):
            continue
        since_open = now - op
        to_close = cl - now
        if since_open < timedelta(minutes=g.reopen_delay_min):
            return CLOSED
        if to_close <= timedelta(minutes=g.close_flat_min):
            return FLAT
        if to_close <= timedelta(minutes=g.close_taker_min):
            return CLOSING_TAKER
        if to_close <= timedelta(minutes=g.close_start_min):
            return CLOSING
        if since_open < timedelta(minutes=g.reopen_delay_min + g.reopen_wide_min):
            return WIDE
        return OPEN
    return CLOSED


def is_roll_window(d: date) -> bool:
    if d.weekday() >= 5:
        return False
    n = 0
    cur = d.replace(day=1)
    while cur <= d:
        if cur.weekday() < 5:
            n += 1
        cur += timedelta(days=1)
    return 5 <= n <= 10
