"""Mode paire pétrole : orchestrateur (cycle, horaires, limites, actifs filtrés) et démarrage
sans HL. Var, flux de prix, horloge et journal sont simulés."""
from datetime import datetime, timezone

import pytest

import main
from arb.config import GlobalCfg, PairCfg
from arb.pair_exec import ExecResult, Fill
import arb.pair_orchestrator as po
from arb.pair_orchestrator import PairOrchestrator
from arb.pair_paper import PaperPairVar
from arb.pricing import VarQuote
from arb.var_client import VarRateLimited, VarRejected

T0 = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc).timestamp()
PX = {"USOILP": 90.0, "UKOILP": 100.0}
PAIR = ("USOILP", "UKOILP")
OTHERS = {"COPPER": {"qty": -0.4, "entry": 5000.0, "liq": 5100.0, "value": -2177.0},
          "COPPP": {"qty": 0.43, "entry": 5000.0, "liq": 4900.0, "value": 2160.0}}


def _iso(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _sess(op, cl):
    return [{"price": "90", "trading_sessions": [{"open": _iso(op), "close": _iso(cl)}]}]


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t

    async def sleep(self, s):
        self.t += max(0.0, s)


class FakeVar:
    """Var simulé avec un vrai état de positions (quantités) et d'autres actifs (cuivre) que le
    bot ne doit jamais toucher. `script` : {n° d'appel market: "reject"}."""

    def __init__(self, pos=None, others=True, sessions=None, script=None, liq=None):
        self.pos = {a: 0.0 for a in PAIR}
        self.pos.update(pos or {})
        self.others = dict(OTHERS) if others else {}
        self.sessions = sessions if sessions is not None else {
            a: _sess(T0 - 7200, T0 + 6 * 3600) for a in PAIR}
        self.script, self.calls, self.orders = dict(script or {}), 0, []
        self.liq = dict(liq or {})
        self.lev = []
        self._quotes = {}
        self.limiter = None

    async def supported_assets(self):
        return self.sessions

    async def set_leverage(self, asset, lev):
        self.lev.append((asset, lev))

    async def portfolio(self):
        return {"balance": 6179.0, "upnl": 0.0}

    async def positions(self, deadline=None):
        out = {a: dict(p) for a, p in self.others.items()}
        for a, q in self.pos.items():
            if q:
                out[a] = {"qty": q, "entry": PX[a], "liq": self.liq.get(a), "value": q * PX[a]}
        return out

    async def quote(self, asset, kind, qty, prio=0, deadline=None):
        assert asset in PAIR, f"cotation sur un actif hors paire : {asset}"
        qid = f"q{len(self._quotes)}"
        self._quotes[qid] = (asset, qty)
        px = PX[asset]
        return VarQuote(bid=px - 0.003, ask=px + 0.003, qty=qty, ts=0, quote_id=qid)

    async def market(self, quote_id, side, max_slippage, reduce_only=False, prio=0, deadline=None):
        i, self.calls = self.calls, self.calls + 1
        asset, qty = self._quotes[quote_id]
        assert asset in PAIR, f"ordre sur un actif hors paire : {asset}"
        if self.script.get(i) == "reject" or self.script.get("all") == "reject":
            raise VarRejected(422, "refus")
        self.pos[asset] = round(self.pos[asset] + (qty if side == "buy" else -qty), 9)
        self.orders.append((asset, side, reduce_only, qty))
        return f"r{i}"

    async def order_status(self, rfq, prio=0, deadline=None):
        return {"status": "cleared", "price": None}


class Stream:
    """Faux flux : dernier prix des deux actifs, horodaté à l'horloge (ou figé si muet)."""

    def __init__(self, clock):
        self.clock = clock
        self.muted_at = None
        self.reconnects = 0

    def force_reconnect(self):
        self.reconnects += 1

    def latest(self, asset):
        ts = self.muted_at if self.muted_at is not None else self.clock()
        return {"mark": PX[asset], "ts_recv_ms": int(ts * 1000)}

    async def run(self, stop):
        await stop.wait()


class J:
    def __init__(self, daily=0.0):
        self.arbs, self.incidents, self.daily = [], [], daily

    def record_arb(self, *a):
        self.arbs.append(a)
        return len(self.arbs)

    def record_incident(self, *a):
        self.incidents.append(a)

    def daily_pnl(self, day=None):
        return self.daily

    def pnl_since(self, ts):
        return sum(r[9] for r in self.arbs), len(self.arbs)

    def volume_since(self, ts, status="pair_fill"):
        return sum(r[6] * r[7] for r in self.arbs if r[11] == status)


class Sig:
    """Signal imposé : sens voulu fixe."""

    def __init__(self, side):
        self._side = side

    def add(self, *a):
        pass

    def ready(self):
        return True

    def z(self):
        return 1.0 if self._side == "A" else -1.0

    def side(self, prev):
        return self._side


async def make(var=None, journal=None, cfg=None, clock=None):
    clock = clock or Clock()
    var = var or FakeVar()
    o = PairOrchestrator(cfg or PairCfg(enabled=True, order_gap_s=0), GlobalCfg(), var,
                         journal or J(), clock=clock, stream=Stream(clock), sleep=clock.sleep)
    await o.setup()
    return o, var, clock


def sides(orders):
    return [(a, s, ro) for a, s, ro, _ in orders]


# 1 ---------------------------------------------------------------------------------------
async def test_first_tick_opens_side_A_with_4_orders():
    o, var, _ = await make()
    await o.tick()
    assert sides(var.orders) == [("USOILP", "sell", False), ("UKOILP", "buy", False)] * 2
    st = o.status()["pair"]
    assert st["state"] == "ACTIF" and st["side"] == "A" and st["cycles"] == 1
    assert st["pos_usd"]["wti"] == pytest.approx(-5000, abs=10)
    assert st["pos_usd"]["brent"] == pytest.approx(5000, abs=10)


# 2 ---------------------------------------------------------------------------------------
async def test_same_side_closes_then_reopens_opposite_flips_and_waits_cycle():
    o, var, clock = await make()
    await o.tick()
    clock.t += 9 * 60
    await o.tick()
    assert len(var.orders) == 4  # 9 min : pas encore de cycle
    clock.t += 60
    await o.tick()
    cyc = var.orders[4:]
    assert len(cyc) == 8
    assert [ro for _, _, ro, _ in cyc] == [True] * 4 + [False] * 4
    assert [s for _, s, _, _ in cyc] == ["buy", "sell"] * 2 + ["sell", "buy"] * 2
    o.signal = Sig("B")
    clock.t += 600
    await o.tick()
    flip = var.orders[12:]
    assert len(flip) == 8
    assert [s for _, s, _, _ in flip] == ["buy", "sell"] * 4
    assert [ro for _, _, ro, _ in flip] == [True] * 4 + [False] * 4
    assert o.status()["pair"]["side"] == "B"
    clock.t += 9 * 60
    await o.tick()
    assert len(var.orders) == 20
    assert var.pos["USOILP"] > 0 > var.pos["UKOILP"]


# 3 ---------------------------------------------------------------------------------------
async def test_copper_positions_never_touched():
    o, var, clock = await make()
    assert o.status()["pair"]["state"] != "BLOQUÉ"
    await o.tick()
    clock.t += 600
    await o.tick()
    await o.kill("test")
    o2, var2, _ = await make()
    await o2.tick()
    assert await o2.close_for_stop(10)
    for v in (var, var2):
        assert {a for a, *_ in v.orders} <= set(PAIR)
        assert {a for a, _ in v.lev} == set(PAIR)
        assert v.others == OTHERS  # positions cuivre intactes
        assert v.pos == {"USOILP": 0.0, "UKOILP": 0.0}


# 4 ---------------------------------------------------------------------------------------
async def test_single_oil_leg_at_start_is_blocked():
    o, var, clock = await make(FakeVar(pos={"USOILP": -30.0}))
    st = o.status()["pair"]
    assert st["state"] == "BLOQUÉ" and "position pétrole existante" in st["reason"]
    await o.tick()
    clock.t += 600
    await o.tick()
    assert var.orders == []


async def test_unequal_legs_at_start_blocked_matched_pair_adopted():
    o, var, _ = await make(FakeVar(pos={"USOILP": -55.0, "UKOILP": 20.0}))
    assert o.status()["pair"]["state"] == "BLOQUÉ"
    o, var, clock = await make(FakeVar(pos={"USOILP": -55.5, "UKOILP": 50.0}))
    st = o.status()["pair"]
    assert st["state"] == "ACTIF" and st["side"] == "A"
    await o.tick()
    assert var.orders == []  # position adoptée : premier cycle après cycle_min
    clock.t += 600
    await o.tick()
    assert len(var.orders) == 8


# 5 ---------------------------------------------------------------------------------------
async def test_no_cycle_15_min_before_close_and_close_at_10():
    close = T0 + 3600
    sess = {a: _sess(T0 - 7200, close) for a in PAIR}
    o, var, clock = await make(FakeVar(sessions=sess))
    clock.t = close - 30 * 60
    await o.tick()
    assert len(var.orders) == 4
    clock.t = close - 12 * 60  # cycle dû (18 min) mais dans les 15 dernières minutes
    await o.tick()
    assert len(var.orders) == 4
    assert o.status()["pair"]["state"] == "ATTENTE"
    clock.t = close - 9 * 60
    await o.tick()
    closing = var.orders[4:]
    assert len(closing) == 4 and all(ro for _, _, ro, _ in closing)
    assert var.pos == {"USOILP": 0.0, "UKOILP": 0.0}
    clock.t += 60
    await o.tick()
    assert len(var.orders) == 8  # plus rien après la fermeture


async def test_reopens_5_min_after_latest_open():
    op = T0 - 60
    sess = {a: _sess(op, op + 6 * 3600) for a in PAIR}
    o, var, clock = await make(FakeVar(sessions=sess))
    await o.tick()
    assert var.orders == []
    clock.t = op + 5 * 60 + 1
    await o.tick()
    assert len(var.orders) == 4


# 6 ---------------------------------------------------------------------------------------
async def test_brent_closed_no_open():
    sess = {"USOILP": _sess(T0 - 7200, T0 + 6 * 3600), "UKOILP": _sess(T0 + 3600, T0 + 8 * 3600)}
    o, var, _ = await make(FakeVar(sessions=sess))
    await o.tick()
    assert var.orders == []
    assert o.status()["pair"]["state"] == "ATTENTE"


# 7 ---------------------------------------------------------------------------------------
async def test_stale_stream_no_cycle():
    o, var, clock = await make()
    o.stream.muted_at = clock.t - 6
    await o.tick()
    assert var.orders == []
    o.stream.muted_at = None
    await o.tick()
    assert len(var.orders) == 4
    o.stream.muted_at = clock.t
    clock.t += 600
    await o.tick()
    assert len(var.orders) == 4


# 8 ---------------------------------------------------------------------------------------
async def test_daily_loss_closes_and_kills():
    j = J()
    o, var, clock = await make(journal=j)
    await o.tick()
    j.daily = -201.0
    clock.t += 60
    await o.tick()
    assert len(var.orders) == 8 and all(ro for _, _, ro, _ in var.orders[4:])
    assert o.killed and o.status()["pair"]["state"] == "ARRÊTÉ"
    assert any("perte" in i[2] for i in j.incidents)
    clock.t += 3600
    await o.tick()
    assert len(var.orders) == 8


# 9 ---------------------------------------------------------------------------------------
async def test_double_failure_mid_cycle_pauses():
    j = J()
    o, var, clock = await make(journal=j)
    await o.tick()
    var.script = {4 + 5: "reject", 4 + 6: "reject"}  # 6e ordre du cycle refusé deux fois
    clock.t += 600
    await o.tick()
    st = o.status()["pair"]
    assert st["state"] == "PAUSE" and "échoué" in st["reason"]
    assert any(i[1] == "error" for i in j.incidents)
    usd = {a: abs(var.pos[a]) * PX[a] for a in PAIR}
    assert abs(usd["USOILP"] - usd["UKOILP"]) <= 2500
    n = len(var.orders)
    clock.t += 600
    await o.tick()
    assert len(var.orders) == n  # PAUSE : plus de cycle
    o.resume()
    assert o.status()["pair"]["state"] == "ACTIF"


async def test_reconciliation_gap_blocks():
    o, var, clock = await make()

    async def fake_run(steps, prices):  # « exécuté » sans que Var ne bouge que la jambe WTI
        var.pos["USOILP"] = -55.0
        return ExecResult(ok=True)

    o.executor.run_steps = fake_run
    await o.tick()
    st = o.status()["pair"]
    assert st["state"] == "BLOQUÉ" and "écart" in st["reason"]


async def test_liquidation_guard_closes_and_pauses():
    j = J()
    o, var, clock = await make(journal=j)
    await o.tick()
    var.liq = {"USOILP": 95.0}  # 5,5 % du prix : sous 8 %
    clock.t += 31
    await o.tick()
    assert len(var.orders) == 8 and all(ro for _, _, ro, _ in var.orders[4:])
    assert o.status()["pair"]["state"] == "PAUSE"
    assert any("liquidation" in i[2] for i in j.incidents)


async def test_liquidation_guard_skips_on_rate_limit():
    o, var, clock = await make()
    await o.tick()
    calls = []

    async def limited(deadline=None):
        calls.append(deadline)
        raise VarRateLimited(5.0)

    var.positions = limited
    clock.t += 31
    await o.tick()  # aucune exception, aucune attente
    assert calls and calls[0] is not None
    assert o.status()["pair"]["state"] == "ACTIF"


# 10 --------------------------------------------------------------------------------------
async def test_close_for_stop_true_when_flat_false_at_timeout():
    steps = []
    o, var, _ = await make()
    o.stop_progress = steps.append
    await o.tick()
    assert await o.close_for_stop(10) is True
    assert "Fermeture de la paire pétrole" in steps and "Vérification des positions" in steps
    o, var, clock = await make()
    await o.tick()
    var.script = {"all": "reject"}
    t0 = clock.t
    assert await o.close_for_stop(10) is False
    assert clock.t - t0 >= 10


# 11 --------------------------------------------------------------------------------------
def _fill(leg, side, qty, px):
    return Fill(leg, side, qty * px, qty, px, px, 0.0, 0.3, 10.0, False)


async def test_realized_pnl_and_cost():
    j = J()
    o, _, _ = await make(journal=j)
    o._apply_fill(_fill("wti", "buy", 27.7, 90.0))
    o._apply_fill(_fill("wti", "sell", 27.7, 90.1))
    st = o.status()["pair"]
    assert st["pnl_session_usd"] == pytest.approx(2.77, abs=1e-6)
    rows = [r for r in j.arbs if r[11] == "pair_pnl"]
    assert len(rows) == 1 and rows[0][9] == pytest.approx(2.77, abs=1e-6)
    assert st["volume_session_usd"] == pytest.approx(27.7 * 180.1)
    assert st["cost_per_m"] == pytest.approx(-2.77 / (27.7 * 180.1) * 1e6, rel=1e-6)


async def test_pnl_short_reduction_and_flip():
    o, _, _ = await make()
    o._apply_fill(_fill("brent", "sell", 10, 100.0))
    o._apply_fill(_fill("brent", "buy", 15, 99.0))  # rachat de 10 (+10 $), puis long de 5 à 99
    assert o.status()["pair"]["pnl_session_usd"] == pytest.approx(10.0)
    assert o._book["brent"] == pytest.approx([5.0, 99.0])


# démarrage sans HL ---------------------------------------------------------------------
async def test_ctx_start_pair_paper_without_hl(tmp_path, monkeypatch):
    from arb.journal import Journal
    monkeypatch.setattr(main, "CONTROL_DIR", tmp_path)
    built = []

    class T:
        def __init__(self, port):
            self.port = port

        async def connect(self):
            pass

        async def close(self):
            pass

    def no_hl(*a, **k):
        raise AssertionError("HLClient construit en mode paire")

    fake = FakeVar(others=True)
    monkeypatch.setattr(main, "CdpTransport", T)
    monkeypatch.setattr(main, "HLClient", no_hl)
    monkeypatch.setattr(main, "VarClient", lambda t, lim: built.append(lim) or fake)

    class A:
        def agent_key_for(self, account):
            return None

    ctx = main.Ctx(GlobalCfg(), [], A(), Journal(tmp_path / "j.db"), paper=True,
                   pair=PairCfg(enabled=True))
    await ctx.start_bot("0xabc")
    assert isinstance(ctx.orch, PairOrchestrator) and ctx.hl is None
    assert isinstance(ctx.orch.var, PaperPairVar) and built
    assert ctx.orch.status()["paper"] is True
    await ctx.stop_bot()
    assert fake.orders == [] and fake.lev == []


# simulateur paper ------------------------------------------------------------------------
async def test_paper_var_simulates_oil_book():
    inner = FakeVar()
    pv = PaperPairVar(inner, PairCfg())
    q = await pv.quote("USOILP", "commodity", 10.0)
    rfq = await pv.market(q.quote_id, "sell", 0.001, False)
    assert (await pv.order_status(rfq))["status"] == "cleared"
    q = await pv.quote("USOILP", "commodity", 25.0)
    with pytest.raises(VarRejected):  # reduce-only au-delà de la position : refusé, rien d'exécuté
        await pv.market(q.quote_id, "buy", 0.001, True)
    q = await pv.quote("USOILP", "commodity", 10.0)
    with pytest.raises(VarRejected):  # reduce-only dans le sens de la position : refusé
        await pv.market(q.quote_id, "sell", 0.001, True)
    q = await pv.quote("USOILP", "commodity", 10.0)
    await pv.market(q.quote_id, "buy", 0.001, True)
    q = await pv.quote("UKOILP", "commodity", 5.0)
    await pv.market(q.quote_id, "buy", 0.001, False)
    pos = await pv.positions()
    assert set(pos) == {"UKOILP"}
    assert pos["UKOILP"]["qty"] == 5.0 and pos["UKOILP"]["liq"] is None
    assert pos["UKOILP"]["value"] == pytest.approx(5.0 * 100.0)
    await pv.set_leverage("USOILP", 5)
    assert inner.orders == [] and inner.lev == []
    assert await pv.supported_assets() == inner.sessions


async def test_paper_var_runs_a_full_cycle():
    clock = Clock()
    inner = FakeVar()
    o = PairOrchestrator(PairCfg(enabled=True, order_gap_s=0), GlobalCfg(), PaperPairVar(inner, PairCfg()),
                         J(), clock=clock, stream=Stream(clock), sleep=clock.sleep)
    await o.setup()
    await o.tick()
    clock.t += 600
    await o.tick()
    st = o.status()["pair"]
    assert st["state"] == "ACTIF" and st["cycles"] == 2 and inner.orders == []
    assert st["pos_usd"]["wti"] == pytest.approx(-5000, abs=10)


def test_journal_volume_since(tmp_path):
    from arb.journal import Journal
    j = Journal(tmp_path / "j.db")
    j.record_arb("USOILP", "", 0, 0, 0, "buy", 90.0, 10.0, 0, 0, 0, "pair_fill")
    j.record_arb("USOILP", "", 0, 0, 0, "sell", 91.0, 10.0, 0, 2.0, 0, "pair_pnl")
    assert j.volume_since(0, "pair_fill") == pytest.approx(900.0)
    assert j.daily_pnl() == pytest.approx(2.0) and j.pnl_since(0)[0] == pytest.approx(2.0)


# round 1 : gardes actives en BLOQUÉ -------------------------------------------------------
CLOSE = T0 + 3600


async def _blocked(kind, j):
    sess = {a: _sess(T0 - 7200, CLOSE) for a in PAIR}
    if kind == "paired":
        o, var, clock = await make(FakeVar(sessions=sess), journal=j)
        await o.tick()
        assert len(var.orders) == 4
        o._set_state("BLOQUÉ", "test")
    else:
        o, var, clock = await make(FakeVar(pos={"USOILP": -30.0}, sessions=sess), journal=j)
    assert o.status()["pair"]["state"] == "BLOQUÉ"
    return o, var, clock


def _flat_by_reduce_only(var, n0):
    new = var.orders[n0:]
    return bool(new) and all(ro for _, _, ro, _ in new) and var.pos == {"USOILP": 0.0, "UKOILP": 0.0}


@pytest.mark.parametrize("kind", ["paired", "single"])
async def test_blocked_session_close_still_flattens(kind):
    o, var, clock = await _blocked(kind, J())
    n0 = len(var.orders)
    clock.t = CLOSE - 9 * 60
    await o.tick()
    assert _flat_by_reduce_only(var, n0)


@pytest.mark.parametrize("kind", ["paired", "single"])
async def test_blocked_liquidation_guard_still_flattens(kind):
    o, var, clock = await _blocked(kind, J())
    n0 = len(var.orders)
    var.liq = {"USOILP": 95.0}
    clock.t += 31
    await o.tick()
    assert _flat_by_reduce_only(var, n0)


@pytest.mark.parametrize("kind", ["paired", "single"])
async def test_blocked_daily_loss_still_flattens_and_kills(kind):
    j = J()
    o, var, clock = await _blocked(kind, j)
    n0 = len(var.orders)
    j.daily = -201.0
    clock.t += 1
    await o.tick()
    assert _flat_by_reduce_only(var, n0)
    assert o.killed


async def test_daily_pnl_unreadable_pauses():
    class BadJ(J):
        def daily_pnl(self, day=None):
            raise RuntimeError("base verrouillée")

    j = BadJ()
    o, var, clock = await make(journal=j)
    await o.tick()
    await o.tick()
    assert var.orders == []
    st = o.status()["pair"]
    assert st["state"] == "PAUSE" and "perte du jour" in st["reason"]
    assert sum("perte du jour illisible" in i[2] for i in j.incidents) == 1


async def test_unknown_sessions_wait_and_close_open_pair():
    j = J()
    o, var, clock = await make(FakeVar(sessions={"USOILP": _sess(T0 - 7200, T0 + 6 * 3600)}), journal=j)
    await o.tick()
    await o.tick()
    assert var.orders == []
    st = o.status()["pair"]
    assert st["state"] == "ATTENTE" and st["reason"] == "séances inconnues"
    assert sum("séances" in i[2] for i in j.incidents) == 1
    # paire ouverte puis séances d'un actif perdues : fermeture reduce-only
    o, var, clock = await make()
    await o.tick()
    o.sessions["UKOILP"] = []
    clock.t += 1
    await o.tick()
    assert _flat_by_reduce_only(var, 4)


async def test_setup_with_unreadable_sessions_does_not_crash():
    var = FakeVar()

    async def boom():
        raise RuntimeError("panne")

    var.supported_assets = boom
    o, var, _ = await make(var)
    await o.tick()
    assert var.orders == [] and o.status()["pair"]["reason"] == "séances inconnues"


async def test_close_prices_use_last_known_price():
    o, var, _ = await make()
    await o.tick()
    seen = []
    real = o.executor.run_steps

    async def spy(steps, prices):
        seen.append(dict(prices))
        return await real(steps, prices)

    o.executor.run_steps = spy
    o.stream.latest = lambda asset: None  # flux perdu
    await o.kill("test")
    assert seen and all(px > 0 for px in seen[0].values())


async def test_ctx_pair_connect_failure_closes_transport(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "CONTROL_DIR", tmp_path)
    closed = []

    class T:
        def __init__(self, port):
            pass

        async def connect(self):
            raise RuntimeError("Chrome absent")

        async def close(self):
            closed.append(True)

    monkeypatch.setattr(main, "CdpTransport", T)
    ctx = main.Ctx(GlobalCfg(), [], None, J(), paper=True, pair=PairCfg(enabled=True))
    with pytest.raises(RuntimeError):
        await ctx.start_bot("0xabc")
    assert closed == [True]


async def test_ctx_pair_stop_labels_do_not_mention_hl(tmp_path, monkeypatch):
    from arb.journal import Journal
    monkeypatch.setattr(main, "CONTROL_DIR", tmp_path)

    class T:
        def __init__(self, port):
            pass

        async def connect(self):
            pass

        async def close(self):
            pass

    monkeypatch.setattr(main, "CdpTransport", T)
    monkeypatch.setattr(main, "VarClient", lambda t, lim: FakeVar())
    ctx = main.Ctx(GlobalCfg(), [], None, Journal(tmp_path / "j.db"), paper=True, pair=PairCfg(enabled=True))
    steps = []
    orig = ctx._stop_step
    ctx._stop_step = lambda s: (steps.append(s), orig(s))
    await ctx.start_bot("0xabc")
    await ctx.close_and_stop()
    labels = [s for s in steps if s]
    assert labels and not any("HL" in s for s in labels)


# relecture finale ------------------------------------------------------------------------
async def test_non_numeric_status_price_in_cycle_blocks_without_order_burst():
    class BadPx(FakeVar):
        async def order_status(self, rfq, prio=0, deadline=None):
            return {"status": "cleared", "price": "n/a"}

    o, var, clock = await make(BadPx())
    await o.tick()  # aucune exception ne remonte
    assert o.state == "BLOQUÉ"
    n = len(var.orders)
    assert n <= 2  # le 1er ordre, plus au plus un rééquilibrage
    for _ in range(10):
        clock.t += 1
        await o.tick()
    assert len(var.orders) == n


async def test_unexpected_error_in_cycle_blocks_reconciles_and_delays():
    j = J()
    o, var, clock = await make(journal=j)
    calls = []

    async def boom(steps, prices):
        calls.append(clock.t)
        raise RuntimeError("panne imprévue")

    o.executor.run_steps = boom
    await o.tick()
    assert o.state == "BLOQUÉ" and "panne imprévue" in o.reason
    assert any("panne imprévue" in i[2] for i in j.incidents)
    assert o._cycle_retry_at > clock.t
    o.resume()  # même débloquée par erreur, la reprise attend le délai
    clock.t += 1
    await o.tick()
    assert len(calls) == 1


async def test_unexpected_error_in_close_blocks_and_waits_before_retry():
    j = J()
    o, var, clock = await make(journal=j)
    await o.tick()  # paire ouverte
    calls = []

    async def boom(steps, prices):
        calls.append(clock.t)
        raise RuntimeError("panne imprévue")

    o.executor.run_steps = boom
    await o.kill("test")  # aucune exception ne remonte
    assert o.state == "BLOQUÉ" and len(calls) == 1
    assert any("panne imprévue" in i[2] for i in j.incidents)
    for _ in range(10):
        clock.t += 1
        await o.tick()
    assert len(calls) == 1  # aucune rafale : un essai toutes les 30 s au plus
    clock.t += 25
    await o.tick()
    assert len(calls) == 2


class StrikeJ(J):
    def __init__(self, strikes=()):
        super().__init__()
        self.strikes, self.recorded = list(strikes), []

    def strikes_since(self, ts):
        return [s for s in self.strikes if s >= ts]

    def record_strike(self, ts=None):
        self.recorded.append(ts)


async def test_var_strikes_loaded_pause_cycles_then_resume():
    from arb.var_limiter import VarLimiter
    clock = Clock()
    var = FakeVar()
    var.limiter = VarLimiter(0.0, 600.0, 3, clock=clock, wall=clock, sleep=clock.sleep)
    j = StrikeJ([T0 - 30, T0 - 20, T0 - 10])
    o, var, clock = await make(var, journal=j, clock=clock)
    assert var.limiter.quotes_suspended()
    await o.tick()
    assert var.orders == []
    st = o.status()["pair"]
    assert st["state"] == "PAUSE" and st["reason"] == "limite Var (strikes)"
    var.limiter.record_limited(0.0)  # un nouveau strike est persisté
    assert j.recorded == [clock.t]
    clock.t += 700  # fin de la suspension : reprise sans intervention
    await o.tick()
    assert len(var.orders) == 4 and o.status()["pair"]["state"] == "ACTIF"


async def test_var_strikes_do_not_prevent_session_close():
    from arb.var_limiter import VarLimiter
    sess = {a: _sess(T0 - 7200, CLOSE) for a in PAIR}
    clock = Clock()
    var = FakeVar(sessions=sess)
    var.limiter = VarLimiter(0.0, 600.0, 3, clock=clock, wall=clock, sleep=clock.sleep)
    o, var, clock = await make(var, journal=StrikeJ(), clock=clock)
    await o.tick()
    assert len(var.orders) == 4
    for _ in range(3):
        var.limiter.record_limited(0.0)
    clock.t = CLOSE - 9 * 60
    await o.tick()
    assert _flat_by_reduce_only(var, 4)


async def test_is_flat_reads_real_positions_and_resyncs_book():
    o, var, _ = await make()
    await o.tick()
    assert await o.is_flat() is False
    o._book = {"wti": [0.0, 0.0], "brent": [0.0, 0.0]}  # suivi faux : à plat
    assert await o.is_flat() is False
    assert o._has_pos()  # recalé sur le réel : la fermeture sera réessayée
    var.pos = {a: 0.0 for a in PAIR}
    o._book = {"wti": [0.0, 0.0], "brent": [0.0, 0.0]}
    assert await o.is_flat() is True


async def test_session_close_resets_signal():
    from arb.pair_signal import RatioSignal
    sess = {a: _sess(T0 - 7200, CLOSE) for a in PAIR}
    o, var, clock = await make(FakeVar(sessions=sess))
    await o.tick()
    assert len(var.orders) == 4
    clock.t = CLOSE - 9 * 60
    o.signal = RatioSignal(1)
    for k in range(70):
        o.signal.add(clock.t - 70 + k, 90.0, 100.0 + k * 0.01)
    assert o.signal.ready()
    await o.tick()
    assert _flat_by_reduce_only(var, 4)
    assert not o.signal.ready() and o.signal.last_ts() is None


async def test_freshness_uses_last_websocket_message():
    o, var, clock = await make()
    o.stream.muted_at = clock.t - 60  # aucun changement de prix depuis 60 s...
    o.stream.last_msg_ms = int(clock.t * 1000)  # ... mais le flux reçoit des messages
    await o.tick()
    assert len(var.orders) == 4
    clock.t += 600
    o.stream.last_msg_ms = int((clock.t - 6) * 1000)  # flux muet depuis 6 s
    await o.tick()
    assert len(var.orders) == 4


async def test_all_sessions_expired_is_unknown_and_closes():
    j = J()
    o, var, clock = await make(journal=j)
    await o.tick()
    assert len(var.orders) == 4
    dt = lambda ts: datetime.fromtimestamp(ts, tz=timezone.utc)
    o.sessions["UKOILP"] = [(dt(T0 - 7200), dt(T0 - 3600))]  # plus aucune séance future
    clock.t += 1
    await o.tick()
    assert _flat_by_reduce_only(var, 4)
    assert any("séances" in i[2] for i in j.incidents)
    assert o.status()["pair"]["reason"] == "séances inconnues"


async def test_unreadable_sessions_at_startup_retried_quickly(monkeypatch):
    import asyncio
    import time
    import arb.pair_orchestrator as po
    monkeypatch.setattr(po, "_SESSIONS_RETRY_S", 0.05)
    var = FakeVar()
    real = var.supported_assets
    n = []

    async def flaky():
        n.append(1)
        if len(n) == 1:
            raise RuntimeError("panne")
        return await real()

    var.supported_assets = flaky
    o = PairOrchestrator(PairCfg(enabled=True, order_gap_s=0), GlobalCfg(), var, J(),
                         clock=time.time, stream=Stream(time.time))
    await o.setup()
    assert not o.sessions.get("USOILP")
    stop = asyncio.Event()
    t = asyncio.create_task(o._refresh_loop(stop))
    await asyncio.sleep(0.3)
    stop.set()
    await asyncio.wait_for(t, 2)
    assert len(n) >= 2 and o.sessions.get("USOILP")


def test_paper_run_uses_separate_journal():
    assert main.journal_path(False) == main.ROOT / "data" / "journal.db"
    assert main.journal_path(True) != main.journal_path(False)
    assert main.journal_path(True).parent == main.ROOT / "data"


# re-relecture ----------------------------------------------------------------------------
async def test_strikes_never_override_or_lift_another_pause():
    from arb.var_limiter import VarLimiter
    j = StrikeJ()
    clock = Clock()
    var = FakeVar()
    var.limiter = VarLimiter(0.0, 600.0, 3, clock=clock, wall=clock, sleep=clock.sleep)
    o, var, clock = await make(var, journal=j, clock=clock)
    await o.tick()
    assert len(var.orders) == 4
    var.liq = {"USOILP": 95.0}  # garde de liquidation : fermeture et PAUSE
    clock.t += 31
    await o.tick()
    assert var.pos == {"USOILP": 0.0, "UKOILP": 0.0}
    st = o.status()["pair"]
    assert st["state"] == "PAUSE" and "liquidation" in st["reason"]
    reason, n = st["reason"], len(var.orders)
    for _ in range(3):
        var.limiter.record_limited(0.0)
    assert var.limiter.quotes_suspended()
    clock.t += 1
    await o.tick()
    assert o.state == "PAUSE" and o.reason == reason
    clock.t += 700  # fin de la suspension
    assert not var.limiter.quotes_suspended()
    await o.tick()
    clock.t += 600
    await o.tick()
    assert o.state == "PAUSE" and o.reason == reason
    assert len(var.orders) == n


async def test_freshness_requires_recent_price_per_asset():
    import arb.pair_orchestrator as po
    o, var, clock = await make()
    o.stream.last_msg_ms = int(clock.t * 1000)  # messages reçus (autre actif)...
    o.stream.muted_at = clock.t - po._ASSET_STALE_S - 1  # ... mais plus aucun prix pétrole depuis 61 s
    await o.tick()
    assert var.orders == []
    o.stream.muted_at = clock.t - po._ASSET_STALE_S + 1
    await o.tick()
    assert len(var.orders) == 4


# Run réel du 09/10 : websocket Var muet de 16:42 à 18:09 sans reconnexion, paire figée en ATTENTE.
async def test_silent_stream_forces_reconnect_with_backoff():
    o, var, clock = await make()
    j = o.journal
    o.stream.muted_at = clock.t
    clock.t += po._WS_RECONNECT_AFTER_S - 1
    await o.tick()
    assert o.stream.reconnects == 0
    clock.t += 2
    await o.tick()
    assert o.stream.reconnects == 1
    assert any("reconnexion" in m for *_, m in j.incidents)
    clock.t += po._WS_RECONNECT_RETRY_S - 1
    await o.tick()
    assert o.stream.reconnects == 1
    clock.t += 2
    await o.tick()
    assert o.stream.reconnects == 2
    o.stream.muted_at = None
    await o.tick()
    assert any("rétabli" in m for *_, m in j.incidents)
