"""L1 (docs/superpowers/levers-2026-09-23.md) : carnet HL rapide par le flux `bbo`."""
from arb.config import GlobalCfg
from arb.hl_feed import HLFeed


class L:
    def __init__(self): self.cbs = []
    def call_soon_threadsafe(self, f, *a): self.cbs.append((f, a))


def _bbo(coin, bid, ask, bsz="1.5", asz="2.0", t=1790176054092):
    b = {"px": str(bid), "sz": bsz, "n": 3} if bid is not None else None
    a = {"px": str(ask), "sz": asz, "n": 2} if ask is not None else None
    return {"channel": "bbo", "data": {"coin": coin, "time": t, "bbo": [b, a]}}


def _l2(coin, bid, ask):
    return {"data": {"coin": coin, "levels": [[{"px": str(bid), "sz": "1"}], [{"px": str(ask), "sz": "1"}]]}}


class FakeInfo:
    def __init__(self): self.subs = []
    def subscribe(self, sub, cb): self.subs.append((sub, cb))


def test_bbo_sets_book_sizes_and_age():
    t = [100.0]
    f = HLFeed(["xyz:CL"], "0xabc", on_fill=lambda x: None, loop=L(), clock=lambda: t[0])
    f.handle_bbo(_bbo("xyz:CL", 91.168, 91.17, bsz="92.895", asz="170.036"))
    assert f.book("xyz:CL") == (91.168, 91.17)
    assert f.book_sizes("xyz:CL") == (92.895, 170.036)
    assert f.book_age("xyz:CL") == 0.0 and f.bbo_age("xyz:CL") == 0.0
    assert f.last_any_msg() == 100.0
    t[0] = 100.4
    assert abs(f.bbo_age("xyz:CL") - 0.4) < 1e-9


def test_bbo_one_side_empty():
    f = HLFeed(["xyz:CL"], "0xabc", on_fill=lambda x: None, loop=L(), clock=lambda: 1.0)
    f.handle_bbo(_bbo("xyz:CL", 91.1, None))
    assert f.book("xyz:CL") == (91.1, None)
    assert f.mid("xyz:CL") is None


def test_bbo_unknown_coin_ignored_for_book_but_counts_as_alive():
    t = [5.0]
    f = HLFeed(["xyz:CL"], "0xabc", on_fill=lambda x: None, loop=L(), clock=lambda: t[0])
    f.handle_bbo(_bbo("xyz:GOLD", 4293.0, 4293.4))
    assert f.book("xyz:CL") == (None, None)
    assert f.book_age("xyz:CL") == float("inf")
    assert f.last_any_msg() == 5.0


def test_stale_l2book_never_overwrites_fresher_bbo():
    """Le bug L1 : un l2Book (qui n'arrive qu'environ toutes les 5 s) ne doit jamais écraser le
    meilleur prix plus frais du flux bbo."""
    t = [100.0]
    f = HLFeed(["xyz:CL"], "0xabc", on_fill=lambda x: None, loop=L(), clock=lambda: t[0])
    f.handle_bbo(_bbo("xyz:CL", 91.20, 91.22))
    t[0] = 100.5
    f.handle_book(_l2("xyz:CL", 91.00, 91.02))
    assert f.book("xyz:CL") == (91.20, 91.22)
    # l'âge du carnet reste celui du dernier bbo.
    assert abs(f.book_age("xyz:CL") - 0.5) < 1e-9


def test_start_subscribes_bbo_not_l2book(monkeypatch):
    import hyperliquid.info as hinfo
    fake = FakeInfo()
    monkeypatch.setattr(hinfo, "Info", lambda **kw: fake)
    f = HLFeed(["xyz:CL", "xyz:GOLD"], "0xabc", on_fill=lambda x: None, loop=L())
    f.start()
    types = [s["type"] for s, _ in fake.subs]
    assert types.count("bbo") == 2
    assert "l2Book" not in types
    assert {"type": "userFills", "user": "0xabc"} in [s for s, _ in fake.subs]
    bbo_cbs = {s["coin"]: cb for s, cb in fake.subs if s["type"] == "bbo"}
    assert bbo_cbs["xyz:CL"] == f.handle_bbo


def test_freshness_defaults_adapted_to_bbo():
    """Le flux bbo arrive environ 10 fois par seconde et par coin (trou maximal observé 5,9 s sur
    l'or) : seuils resserrés par rapport à ceux calibrés sur l2Book (15 s et 30 s)."""
    g = GlobalCfg()
    assert g.hl_ws_stall_s == 10.0
    assert g.hl_book_max_age_s == 10.0
