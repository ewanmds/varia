from datetime import datetime, date, timezone, timedelta
from arb.config import GlobalCfg
from arb.schedule import parse_sessions, phase_at, is_roll_window, OPEN, WIDE, CLOSING, CLOSING_TAKER, FLAT, CLOSED

UTC = timezone.utc
G = GlobalCfg()
ENTRY = {"trading_sessions": [
    {"open": "2026-09-21T22:00:00Z", "close": "2026-09-22T21:00:00Z"},
    {"open": "2026-09-22T22:00:00Z", "close": "2026-09-23T21:00:00Z"},
]}

def t(h, m, day=22):
    return datetime(2026, 9, day, h, m, tzinfo=UTC)

def test_parse_sessions():
    s = parse_sessions(ENTRY)
    assert s[0][0] == datetime(2026, 9, 21, 22, 0, tzinfo=UTC)
    assert s[1][1] == datetime(2026, 9, 23, 21, 0, tzinfo=UTC)

def test_phases_around_close():
    s = parse_sessions(ENTRY)
    assert phase_at(t(12, 0), s, G) == OPEN
    assert phase_at(t(20, 41), s, G) == CLOSING
    assert phase_at(t(20, 51), s, G) == CLOSING_TAKER
    assert phase_at(t(20, 56), s, G) == FLAT
    assert phase_at(t(21, 30), s, G) == CLOSED

def test_phases_after_open():
    s = parse_sessions(ENTRY)
    assert phase_at(t(22, 2), s, G) == CLOSED      # délai de réouverture de 5 min
    assert phase_at(t(22, 10), s, G) == WIDE       # 15 min de marge doublée
    assert phase_at(t(22, 25), s, G) == OPEN

def test_roll_window():
    # septembre 2026 : 1er = mardi. Jours ouvrés : 1,2,3,4,7(5e),8,9,10,11(9e),14(10e)
    assert not is_roll_window(date(2026, 9, 4))
    assert is_roll_window(date(2026, 9, 7))
    assert is_roll_window(date(2026, 9, 14))
    assert not is_roll_window(date(2026, 9, 15))
    assert not is_roll_window(date(2026, 9, 12))  # samedi
