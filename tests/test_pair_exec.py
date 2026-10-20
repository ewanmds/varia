from arb.pair_exec import plan_cycle, target_usd

def legs(steps):
    return [(s[0], s[1], s[2], s[3]) for s in steps]

def test_target():
    assert target_usd("A", 5000) == {"wti": -5000, "brent": 5000}
    assert target_usd("B", 5000) == {"wti": 5000, "brent": -5000}

def test_first_open_A():
    steps = plan_cycle({"wti": 0, "brent": 0}, target_usd("A", 5000), 2500, reopen=False)
    assert legs(steps) == [("wti", "sell", 2500, False), ("brent", "buy", 2500, False),
                           ("wti", "sell", 2500, False), ("brent", "buy", 2500, False)]

def test_flip_A_to_B_is_8_alternating_orders():
    steps = plan_cycle(target_usd("A", 5000), target_usd("B", 5000), 2500, reopen=False)
    assert [s[0] for s in steps] == ["wti", "brent"] * 4
    assert [s[1] for s in steps] == ["buy", "sell"] * 4
    assert [s[3] for s in steps] == [True, True, True, True, False, False, False, False]
    assert sum(s[2] for s in steps) == 20000

def test_reopen_same_side_closes_then_reopens():
    steps = plan_cycle(target_usd("A", 5000), target_usd("A", 5000), 2500, reopen=True)
    assert [s[1] for s in steps] == ["buy", "sell", "buy", "sell", "sell", "buy", "sell", "buy"]
    assert [s[3] for s in steps] == [True] * 4 + [False] * 4
    assert sum(s[2] for s in steps) == 20000

def test_imbalance_never_exceeds_one_tranche():
    steps = plan_cycle(target_usd("A", 5000), target_usd("B", 5000), 2500, reopen=False)
    pos = {"wti": -5000, "brent": 5000}
    for leg, side, usd, _ in steps:
        pos[leg] += usd if side == "buy" else -usd
        assert abs(abs(pos["wti"]) - abs(pos["brent"])) <= 2500


import pytest
from arb.config import PairCfg
from arb.pair_exec import PairExecutor
from arb.pricing import VarQuote
from arb.var_client import VarError, VarRejected

PX = {"USOILP": 90.0, "UKOILP": 100.0}
PRICES = {"wti": 90.0, "brent": 100.0}
ASSET = {"wti": "USOILP", "brent": "UKOILP"}


class FakeVar:
    """Var simulé avec un vrai état de positions. `script` : {n° d'appel market: comportement}.
    reject : refus 422 (rien n'est exécuté) ; pending : exécuté mais statut toujours en attente ;
    status_err_applied / status_err_dropped : order_status lève, ordre exécuté / non exécuté ;
    market_5xx_applied : market lève un 500 alors que l'ordre est passé."""

    def __init__(self, start_usd=None, script=None):
        self._delayed = None
        self.delay = 3
        start_usd = start_usd or {"wti": 0.0, "brent": 0.0}
        self.pos = {ASSET[k]: round(v / PRICES_BY_LEG[k], 6) for k, v in start_usd.items()}
        self.script, self.calls, self.orders = dict(script or {}), 0, []
        self._quotes, self._rfq = {}, {}

    async def positions(self, deadline=None):
        if self._delayed:
            asset, delta, left = self._delayed
            left -= 1
            self._delayed = (asset, delta, left) if left else None
            if not left:
                self.pos[asset] = round(self.pos[asset] + delta, 9)
        return {a: {"qty": q} for a, q in self.pos.items()}

    async def quote(self, asset, kind, qty, prio=0, deadline=None):
        px = PX[asset]
        qid = f"q{len(self._quotes)}"
        self._quotes[qid] = (asset, qty)
        return VarQuote(bid=px - 0.003, ask=px + 0.003, qty=qty, ts=0, quote_id=qid)

    async def market(self, quote_id, side, max_slippage, reduce_only=False, prio=0, deadline=None):
        i, self.calls = self.calls, self.calls + 1
        beh = self.script.get(i)
        if beh == "reject":
            raise VarRejected(422, "refus")
        asset, qty = self._quotes[quote_id]
        sign = 1 if side == "buy" else -1
        if beh == "delayed":  # position appliquée après 3 lectures, order_status lève
            self._delayed = (asset, sign * qty, self.delay)
            self.orders.append((asset, side, reduce_only, qty))
        elif beh == "partial":  # 10 % exécutés, statut cleared
            self.pos[asset] = round(self.pos[asset] + sign * qty * 0.1, 9)
            self.orders.append((asset, side, reduce_only, qty))
        elif beh != "status_err_dropped":
            self.pos[asset] = round(self.pos[asset] + sign * qty, 9)
            self.orders.append((asset, side, reduce_only, qty))
        self._rfq[f"r{i}"] = beh
        if beh == "market_5xx_applied":
            raise VarRejected(503, "timeout")
        return f"r{i}"

    async def order_status(self, rfq, prio=0, deadline=None):
        beh = self._rfq[rfq]
        if beh == "pending":
            return {"status": "pending"}
        if beh in ("status_err_applied", "status_err_dropped", "delayed"):
            raise VarError("boom")
        return {"status": "cleared", "price": None}


PRICES_BY_LEG = {"wti": 90.0, "brent": 100.0}


class J:
    def __init__(self): self.rows = []
    def record_arb(self, *a): self.rows.append(a); return 1
    def record_incident(self, *a): pass


async def nosleep(s): pass


@pytest.mark.parametrize("size,slices", [(20, [20]), (50, [50]), (750, [500, 250]), (1000, [500, 500])])
async def test_small_legs_open_flip_recycle_and_close(size, slices):
    cfg = PairCfg(leg_usd=size, tranche_usd=500, order_gap_s=0)
    var = FakeVar()
    executor = PairExecutor(var, cfg, J(), sleep=nosleep)
    current = {"wti": 0.0, "brent": 0.0}
    for target, reopen in [(target_usd("A", size), False),
                           (target_usd("B", size), False),
                           (target_usd("B", size), True),
                           ({"wti": 0.0, "brent": 0.0}, True)]:
        steps = plan_cycle(current, target, cfg.effective_tranche_usd, reopen)
        for leg in ("wti", "brent"):
            openings = [s.usd for s in steps if s.leg == leg and not s.reduce_only]
            assert openings == (slices if target[leg] else [])
        result = await executor.run_steps(steps, PRICES)
        assert result.ok, result.reason
        actual = usd_pos(var)
        for leg in target:
            assert actual[leg] == pytest.approx(target[leg], abs=0.2)
        current = target


async def test_failed_small_leg_hedge_rebalances_instead_of_leaving_one_leg():
    cfg = PairCfg(leg_usd=20, tranche_usd=500, order_gap_s=0)
    var = FakeVar(script={1: "reject", 2: "reject"})
    executor = PairExecutor(var, cfg, J(), sleep=nosleep)
    steps = plan_cycle({"wti": 0, "brent": 0}, target_usd("A", 20),
                       cfg.effective_tranche_usd, False)
    result = await executor.run_steps(steps, PRICES)
    assert not result.ok
    assert len(var.orders) == 2
    assert var.orders[-1][2] is True
    assert all(abs(value) < 0.1 for value in usd_pos(var).values())


def ex(var):
    return PairExecutor(var, PairCfg(order_gap_s=0), J(), sleep=nosleep)


def usd_pos(var):
    return {k: var.pos[ASSET[k]] * PRICES_BY_LEG[k] for k in ASSET}


def assert_balanced(var):
    u = usd_pos(var)
    assert abs(abs(u["wti"]) - abs(u["brent"])) <= 0.1 * 2500 + 1e-6, u


PAIR2 = [("wti", "sell", 2500, False), ("brent", "buy", 2500, False)]
A, B = target_usd("A", 5000), target_usd("B", 5000)
FLIP = plan_cycle(A, B, 2500, reopen=False)


async def test_flip_executes_8_orders_and_logs_fills():
    var = FakeVar(A)
    e = ex(var)
    res = await e.run_steps(FLIP, PRICES)
    assert res.ok and len(res.filled) == 8 and len(var.orders) == 8
    assert all(f.half_spread_bps > 0 for f in res.filled)
    assert len(e.journal.rows) == 8 and e.journal.rows[0][-1] == "pair_fill"
    assert usd_pos(var)["wti"] == pytest.approx(5000, abs=1) and usd_pos(var)["brent"] == pytest.approx(-5000, abs=1)


async def test_exec_price_from_status_gives_slippage():
    class Px(FakeVar):
        async def order_status(self, rfq, prio=0, deadline=None):
            return {"status": "cleared", "price": 89.0}
    var = Px()
    res = await ex(var).run_steps(PAIR2, PRICES)
    assert res.filled[0].exec_px == 89.0 and res.filled[0].slip_bps > 0


async def test_close_in_quantity_ends_exactly_at_zero():
    var = FakeVar()
    var.pos = {"USOILP": -55.5557, "UKOILP": 49.9993}  # quantités qui ne tombent pas rond en $
    steps = plan_cycle(A, A, 2500, reopen=True)[:4]
    res = await ex(var).run_steps(steps, PRICES)
    assert res.ok and var.pos["USOILP"] == 0 and var.pos["UKOILP"] == 0


async def test_one_failure_is_retried():
    var = FakeVar(A, {3: "reject"})
    res = await ex(var).run_steps(FLIP, PRICES)
    assert res.ok and len(res.filled) == 8 and len(var.orders) == 8


async def test_rate_limited_waits_at_least_wait_s():
    from arb.var_client import VarRateLimited

    class RL(FakeVar):
        raised = False
        async def quote(self, *a, **k):
            if not RL.raised:
                RL.raised = True
                raise VarRateLimited(7.0)
            return await super().quote(*a, **k)

    waits = []
    async def rec(s): waits.append(s)
    e = PairExecutor(RL(), PairCfg(order_gap_s=0), J(), sleep=rec)
    res = await e.run_steps(PAIR2, PRICES)
    assert res.ok and 7.0 in waits


@pytest.mark.parametrize("beh", ["pending", "status_err_applied", "market_5xx_applied"])
async def test_uncertain_but_executed_is_never_resent(beh):
    var = FakeVar(A, {2: beh})
    res = await ex(var).run_steps(FLIP, PRICES)
    assert res.ok and len(res.filled) == 8 and len(var.orders) == 8 and var.calls == 8


async def test_unconfirmed_order_is_not_resent_and_is_rebalanced():
    # ordre jamais passé, order_status lève : rien de confirmé dans la fenêtre, pas de renvoi
    var = FakeVar(A, {2: "status_err_dropped"})
    res = await ex(var).run_steps(FLIP, PRICES)
    assert not res.ok and var.calls <= 4  # 3 ordres envoyés + au plus 1 de rééquilibrage
    assert res.reason.startswith("déséquilibre")
    assert_balanced(var)


async def test_late_position_with_failing_status_is_not_resent():
    var = FakeVar(A, {2: "delayed"})
    res = await ex(var).run_steps(FLIP, PRICES)
    assert res.ok and len(res.filled) == 8 and var.calls == 8


async def test_partial_fill_counted_as_complete_triggers_rebalance():
    var = FakeVar(A, {0: "partial"})
    res = await ex(var).run_steps(FLIP, PRICES)
    assert not res.ok and res.reason
    assert_balanced(var)


@pytest.mark.parametrize("k", range(8))
async def test_double_failure_rebalances_at_any_position(k):
    var = FakeVar(A, {k: "reject", k + 1: "reject"})
    res = await ex(var).run_steps(FLIP, PRICES)
    assert not res.ok
    assert_balanced(var)


async def test_double_failure_from_unbalanced_start_ends_balanced():
    start = {"wti": -5000.0, "brent": 2500.0}
    steps = plan_cycle(start, B, 2500, reopen=False)
    var = FakeVar(start, {1: "reject", 2: "reject"})
    res = await ex(var).run_steps(steps, PRICES)
    assert not res.ok
    assert_balanced(var)


async def test_uncertain_failure_then_failed_retry_does_not_leave_a_lone_leg():
    # brent sell "pending" qui finit exécuté, puis l'ordre suivant échoue deux fois
    var = FakeVar(A, {1: "pending", 2: "reject", 3: "reject"})
    res = await ex(var).run_steps(FLIP, PRICES)
    assert not res.ok
    assert_balanced(var)
    assert min(abs(v) for v in usd_pos(var).values()) > 0


async def test_failed_rebalance_reports_imbalance():
    var = FakeVar(A, {k: "reject" for k in range(1, 20)})
    res = await ex(var).run_steps(FLIP, PRICES)
    assert not res.ok and res.reason.startswith("déséquilibre")


async def test_order_settling_in_second_window_leaves_no_lone_leg():
    # le brent sell (ordre 2) se règle après la 1re fenêtre (12 lectures) mais pendant la 2e
    var = FakeVar(A, {1: "delayed"})
    var.delay = 20
    res = await ex(var).run_steps(FLIP, PRICES)
    assert not res.ok and res.reason.startswith("déséquilibre")
    assert_balanced(var)
    assert min(abs(v) for v in usd_pos(var).values()) > 0
    assert var.calls == 2  # aucun renvoi de l'ordre incertain


async def test_order_never_settling_reports_imbalance():
    var = FakeVar(A, {1: "status_err_dropped"})
    res = await ex(var).run_steps(FLIP, PRICES)
    assert not res.ok and res.reason.startswith("déséquilibre")


async def test_valuation_uses_value_and_retries_rate_limit():
    from arb.var_client import VarRateLimited

    class V(FakeVar):
        n = 0
        async def positions(self, deadline=None):
            V.n += 1
            if V.n == 1:
                raise VarRateLimited(2.0)
            p = await super().positions()
            return {a: {"qty": d["qty"], "value": d["qty"] * PX[a]} for a, d in p.items()}
        async def quote(self, *a, **k):
            raise AssertionError("la valorisation doit passer par value/qty")

    var = V(A)
    e = ex(var)
    v = await e._valued_positions()
    assert v["wti"][1] == pytest.approx(90.0) and v["brent"][1] == pytest.approx(100.0)


# relecture finale ------------------------------------------------------------------------
async def test_non_numeric_status_price_takes_unknown_path_without_resend():
    # ordre exécuté, mais statut « cleared » avec un prix illisible : issue inconnue (seconde
    # fenêtre, rééquilibrage), jamais de renvoi ni d'exception qui remonte
    class BadPx(FakeVar):
        async def order_status(self, rfq, prio=0, deadline=None):
            return {"status": "cleared", "price": "n/a"}

    var = BadPx(A)
    res = await ex(var).run_steps(FLIP, PRICES)
    assert not res.ok and res.reason.startswith("déséquilibre")
    assert [a for a, *_ in var.orders].count("USOILP") == 1  # l'ordre incertain n'est jamais renvoyé
    assert var.calls <= 2  # plus au plus un ordre de rééquilibrage
    assert res.filled and res.filled[0].leg == "wti"  # compté par la seconde fenêtre
    assert_balanced(var)


async def test_unexpected_error_in_fill_is_handled_as_unknown():
    # division par zéro dans _fill (cotation à 0) : même chemin qu'une issue inconnue
    class Zero(FakeVar):
        async def quote(self, asset, kind, qty, prio=0, deadline=None):
            q = await super().quote(asset, kind, qty, prio, deadline)
            return VarQuote(bid=0.0, ask=0.0, qty=qty, ts=0, quote_id=q.quote_id)

    var = Zero(A)
    res = await ex(var).run_steps(FLIP, PRICES)
    assert not res.ok and res.reason.startswith("déséquilibre")
    assert var.calls == 1


async def test_small_final_gap_from_price_move_is_tolerated():
    # mouvement relatif WTI/Brent de 4 % pendant le cycle : écart final d'environ 100 $, sous
    # 0,1 tranche (250 $) : pas de rééquilibrage, pas de PAUSE
    class Move(FakeVar):
        async def quote(self, asset, kind, qty, prio=0, deadline=None):
            q = await super().quote(asset, kind, qty, prio, deadline)
            if self.calls >= 2 and asset == "UKOILP":
                px = PX[asset] * 1.04
                return VarQuote(bid=px - 0.003, ask=px + 0.003, qty=qty, ts=0, quote_id=q.quote_id)
            return q

    var = Move()
    res = await ex(var).run_steps(PAIR2, PRICES)
    assert res.ok and var.calls == 2
