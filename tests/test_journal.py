from datetime import date, datetime, timezone
from arb.journal import Journal

def test_roundtrip(tmp_path):
    j = Journal(tmp_path / "j.db")
    j.record_arb("XAGS", "B", 67.0, 15, 0.01, "sell", 67.05, 15, 7.3, 0.74, 420, "hedged")
    j.record_arb("XAGS", "A", 67.2, 15, 0.01, "buy", 67.25, 15, -7.4, -0.75, 300, "unwound")
    j.record_incident("XAGS", "warn", "429 sur cotation")
    today = datetime.now(timezone.utc).date()
    assert round(j.daily_pnl(today), 2) == -0.01
    assert j.recent_arbs()[0]["status"] == "unwound"
    assert j.recent_incidents()[0]["msg"] == "429 sur cotation"
    p = j.export_csv(today, tmp_path)
    assert p.exists() and "XAGS" in p.read_text()


def test_pnl_since(tmp_path):
    import time
    j = Journal(tmp_path / "s.db")
    j.record_arb("XAUS", "B", 1, 1, 0, "sell", 1, 1, 1.0, 0.5, 10, "hedged")
    t = time.time() + 0.01
    time.sleep(0.02)
    j.record_arb("XAUS", "A", 1, 1, 0, "buy", 1, 1, 1.0, 0.25, 10, "hedged")
    assert j.pnl_since(t) == (0.25, 1)
