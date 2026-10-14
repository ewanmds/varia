import asyncio
from arb.hl_feed import HLFeed

class L:
    def __init__(self): self.cbs = []
    def call_soon_threadsafe(self, f, *a): self.cbs.append((f, a))

def test_book_and_stall():
    t = [100.0]
    f = HLFeed(["xyz:SILVER"], "0xabc", on_fill=lambda x: None, loop=L(), clock=lambda: t[0])
    f.handle_book({"data": {"coin": "xyz:SILVER", "levels": [[{"px": "67.0", "sz": "10"}], [{"px": "67.2", "sz": "5"}]]}})
    assert f.book("xyz:SILVER") == (67.0, 67.2)
    assert f.mid("xyz:SILVER") == 67.1
    assert not f.stalled(2.0)
    t[0] = 103.0
    assert f.stalled(2.0)

def test_last_any_msg_and_book_age():
    t = [100.0]
    f = HLFeed(["xyz:SILVER", "xyz:GOLD"], "0xabc", on_fill=lambda x: None, loop=L(), clock=lambda: t[0])
    # rien reçu encore : book_age infini pour les deux coins.
    assert f.book_age("xyz:SILVER") == float("inf")
    assert f.book_age("xyz:GOLD") == float("inf")
    f.handle_book({"data": {"coin": "xyz:SILVER", "levels": [[{"px": "67.0", "sz": "10"}], [{"px": "67.2", "sz": "5"}]]}})
    assert f.last_any_msg() == 100.0
    assert f.book_age("xyz:SILVER") == 0.0
    # GOLD n'a toujours rien reçu : son carnet reste périmé, indépendamment de SILVER.
    assert f.book_age("xyz:GOLD") == float("inf")
    t[0] = 130.0  # 30 s plus tard, aucun nouveau message.
    assert f.book_age("xyz:SILVER") == 30.0
    # un message sur N'IMPORTE QUEL canal (ici un fill) fait avancer last_any_msg().
    f.handle_fills({"data": {"fills": [{"coin": "xyz:SILVER", "side": "B", "px": "67.0", "sz": "1",
                                        "oid": 1, "fee": "0", "tid": 1, "time": 1}]}})
    assert f.last_any_msg() == 130.0
    # ... mais ne rafraîchit pas le carnet lui-même (book_age reste basé sur le dernier l2Book).
    assert f.book_age("xyz:SILVER") == 30.0


def test_last_any_msg_advances_on_fill_snapshot_too():
    t = [5.0]
    f = HLFeed(["xyz:SILVER"], "0xabc", on_fill=lambda x: None, loop=L(), clock=lambda: t[0])
    f.handle_fills({"data": {"isSnapshot": True, "fills": []}})
    assert f.last_any_msg() == 5.0


def test_fills_skip_snapshot_and_dispatch():
    loop = L(); got = []
    f = HLFeed(["xyz:SILVER"], "0xabc", on_fill=got.append, loop=loop, clock=lambda: 1.0)
    f.handle_fills({"data": {"isSnapshot": True, "fills": [{"coin": "xyz:SILVER"}]}})
    assert loop.cbs == []
    f.handle_fills({"data": {"fills": [{"coin": "xyz:SILVER", "side": "B", "px": "67.0", "sz": "2",
                                        "oid": 5, "fee": "0.01", "tid": 9, "time": 1}]}})
    fn, args = loop.cbs[0]; fn(*args)
    assert got[0]["sz"] == 2.0 and got[0]["side"] == "B" and got[0]["oid"] == 5
