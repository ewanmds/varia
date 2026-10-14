from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from arb.engine import BLOCKED, IDLE, QUOTING
from arb.server import create_app
from arb.var_errors import VarError


class Cfg:
    def __init__(self, enabled=True, min_edge_bps=5.0, fill_usd=1000.0, max_position_usd=3000.0):
        self.enabled = enabled
        self.min_edge_bps = min_edge_bps
        self.fill_usd = fill_usd
        self.max_position_usd = max_position_usd


class Engine:
    def __init__(self, state=QUOTING):
        self.cfg = Cfg()
        self.state = state
        self.cancelled = 0
        self.resumed = 0

    async def cancel_all(self):
        self.cancelled += 1

    def resume(self):
        self.resumed += 1
        self.state = QUOTING


class Orch:
    def __init__(self):
        self.killed = False
        self.kill_reason = None
        self.engines = {"BTC": Engine()}

    def status(self):
        return {"markets": [], "killed": self.killed}

    async def kill(self, reason):
        self.killed = True
        self.kill_reason = reason


class Journal:
    def recent_arbs(self, n=30):
        return []

    def recent_incidents(self, n=30):
        return []


class OkAuth:
    """Vague finale I7 : session wallet valide seulement avec le jeton "tok"."""

    def require_auth(self, token, account):
        if token != "tok":
            raise RuntimeError("no session")


def _client(ctx):
    # Vague finale I7 : Host local (TrustedHostMiddleware) et jeton de session par défaut.
    return TestClient(create_app(ctx), base_url="http://127.0.0.1", headers={"x-auth-token": "tok"})


class Ctx:
    def __init__(self):
        self.orch = Orch()
        self.journal = Journal()
        self.auth = OkAuth()
        self.paper = True


# -- brief step 1 (kept verbatim) --------------------------------------------

def test_status_and_kill():
    ctx = Ctx()
    c = _client(ctx)
    assert c.get("/api/status").json()["markets"] == []
    assert c.post("/api/bot/kill").status_code == 200
    assert ctx.orch.killed


# -- status ---------------------------------------------------------------

def test_status_without_orchestrator_returns_empty_markets():
    class NoOrchCtx(Ctx):
        def __init__(self):
            self.orch = None
            self.journal = Journal()
            self.auth = OkAuth()
            self.paper = True

    c = _client(NoOrchCtx())
    body = c.get("/api/status").json()
    assert body["markets"] == []
    assert body["running"] is False


def test_status_reports_paper_flag():
    ctx = Ctx()
    ctx.paper = False
    c = _client(ctx)
    assert c.get("/api/status").json()["paper"] is False


def test_status_mode_pair_before_start_and_only_pair_orders():
    class PairJournal(Journal):
        def recent_arbs(self, n=30):
            return [{"status": "pair_fill", "market": "USOILP"}, {"status": "exit", "market": "UKOILP"},
                    {"status": "pair_pnl", "market": "USOILP"}]

    class PairCtx(Ctx):
        def __init__(self):
            super().__init__()
            self.orch = None
            self.journal = PairJournal()

        def _pair_mode(self):
            return True

    ctx = PairCtx()
    ctx.journal.recent_incidents = lambda n=30: [{"ts": 1.0, "message": "ancien"}, {"ts": 4e9, "message": "nouveau"}]
    body = _client(ctx).get("/api/status").json()
    assert body["mode"] == "pair"
    assert [i["message"] for i in body["incidents"]] == ["nouveau"]
    assert body["arbs"] == [{"status": "pair_fill", "market": "USOILP"}]


def test_status_mode_arb_by_default():
    assert _client(Ctx()).get("/api/status").json()["mode"] == "arb"


def test_kill_without_orchestrator_is_400():
    class NoOrchCtx(Ctx):
        def __init__(self):
            self.orch = None
            self.journal = Journal()
            self.auth = OkAuth()
            self.paper = True

    c = _client(NoOrchCtx())
    assert c.post("/api/bot/kill").status_code == 400


# -- amendment t13 §2 : require_auth avant toute action sensible -------------

def test_approve_submit_without_token_is_400_and_does_not_call_module_function(monkeypatch):
    ctx = Ctx()

    class FakeAuth:
        def require_auth(self, token, account):
            raise RuntimeError("Wallet signature missing or expired. Please sign again.")

    ctx.auth = FakeAuth()
    c = _client(ctx)

    called = {"n": 0}

    def fake_approve_submit(*a, **k):
        called["n"] += 1
        return {}

    monkeypatch.setattr("arb.server.authmod.approve_submit", fake_approve_submit)

    res = c.post("/api/agent/approve_submit", json={"account_address": "0x" + "ab" * 20,
                                                     "nonce": 1, "signature": "0x" + "11" * 65})
    assert res.status_code == 400
    assert called["n"] == 0


def test_approve_challenge_without_token_is_400():
    ctx = Ctx()

    class FakeAuth:
        def require_auth(self, token, account):
            raise RuntimeError("no session")

    ctx.auth = FakeAuth()
    c = _client(ctx)
    res = c.post("/api/agent/approve_challenge", json={"account_address": "0x" + "ab" * 20})
    assert res.status_code == 400


def test_bot_start_without_token_is_400_and_start_bot_not_called():
    class StartCtx(Ctx):
        def __init__(self):
            super().__init__()
            self.orch = None
            self.calls = 0

        async def start_bot(self, account):
            self.calls += 1

    ctx = StartCtx()

    class FakeAuth:
        def require_auth(self, token, account):
            raise RuntimeError("no session")

    ctx.auth = FakeAuth()
    c = _client(ctx)
    res = c.post("/api/bot/start", json={"account_address": "0x" + "ab" * 20})
    assert res.status_code == 400
    assert ctx.calls == 0


def test_bot_start_with_valid_token_calls_start_bot():
    class StartCtx(Ctx):
        def __init__(self):
            super().__init__()
            self.orch = None
            self.calls = []

        async def start_bot(self, account):
            self.calls.append(account)

    ctx = StartCtx()

    class FakeAuth:
        def require_auth(self, token, account):
            assert token == "tok123"

    ctx.auth = FakeAuth()
    c = _client(ctx)
    res = c.post("/api/bot/start", headers={"x-auth-token": "tok123"},
                 json={"account_address": "0x" + "ab" * 20})
    assert res.status_code == 200
    assert ctx.calls == ["0x" + "ab" * 20]


# -- market toggle / params ---------------------------------------------------

def test_toggle_unknown_market_is_404():
    ctx = Ctx()
    c = _client(ctx)
    assert c.post("/api/market/DOGE/toggle").status_code == 404


def test_toggle_disables_cancels_orders_and_sets_idle():
    ctx = Ctx()
    c = _client(ctx)
    res = c.post("/api/market/BTC/toggle")
    assert res.status_code == 200
    assert res.json()["enabled"] is False
    eng = ctx.orch.engines["BTC"]
    assert eng.cancelled == 1
    assert eng.state == IDLE


def test_toggle_reenables_calls_resume():
    ctx = Ctx()
    eng = ctx.orch.engines["BTC"]
    eng.cfg.enabled = False
    eng.state = IDLE
    c = _client(ctx)
    res = c.post("/api/market/BTC/toggle")
    assert res.status_code == 200
    assert res.json()["enabled"] is True
    assert eng.resumed == 1
    assert eng.state == QUOTING


def test_toggle_off_does_not_touch_open_positions():
    # amendement t13 §4 : la désactivation annule les ordres mais ne débouble jamais les
    # positions ouvertes (pas d'appel à flatten depuis l'endpoint).
    ctx = Ctx()
    eng = ctx.orch.engines["BTC"]
    eng.pos_hl = 1.0
    eng.pos_var = -1.0
    c = _client(ctx)
    c.post("/api/market/BTC/toggle")
    assert eng.pos_hl == 1.0
    assert eng.pos_var == -1.0


def test_params_unknown_market_is_404():
    ctx = Ctx()
    c = _client(ctx)
    assert c.post("/api/market/DOGE/params", json={}).status_code == 404


def test_params_updates_valid_values():
    ctx = Ctx()
    c = _client(ctx)
    res = c.post("/api/market/BTC/params", json={"min_edge_bps": 8, "fill_usd": 500, "max_position_usd": 2000})
    assert res.status_code == 200
    eng = ctx.orch.engines["BTC"]
    assert eng.cfg.min_edge_bps == 8
    assert eng.cfg.fill_usd == 500
    assert eng.cfg.max_position_usd == 2000


def test_params_rejects_non_positive_fill_usd():
    ctx = Ctx()
    c = _client(ctx)
    res = c.post("/api/market/BTC/params", json={"fill_usd": 0})
    assert res.status_code == 400


def test_params_rejects_min_edge_bps_out_of_range():
    ctx = Ctx()
    c = _client(ctx)
    res = c.post("/api/market/BTC/params", json={"min_edge_bps": 150})
    assert res.status_code == 400


def test_params_min_edge_bps_bounded_1_to_20():
    # la marge par marché est bornée à [1, 20] pb, comme le curseur (borne basse abaissée de 3 à 1 le 23/09).
    ctx = Ctx()
    c = _client(ctx)
    for bad in (0, 0.9, 20.5):
        assert c.post("/api/market/BTC/params", json={"min_edge_bps": bad}).status_code == 400
    assert ctx.orch.engines["BTC"].cfg.min_edge_bps == 5.0
    for ok in (1, 1.5, 20):
        assert c.post("/api/market/BTC/params", json={"min_edge_bps": ok}).status_code == 200
        assert ctx.orch.engines["BTC"].cfg.min_edge_bps == ok


def test_params_rejects_fill_usd_above_max_position_usd():
    ctx = Ctx()
    c = _client(ctx)
    res = c.post("/api/market/BTC/params", json={"fill_usd": 5000})
    assert res.status_code == 400


def test_params_rejects_non_numeric_value():
    ctx = Ctx()
    c = _client(ctx)
    res = c.post("/api/market/BTC/params", json={"fill_usd": "beaucoup"})
    assert res.status_code == 400


# -- bot/stop ---------------------------------------------------------------

def test_bot_stop_calls_ctx_stop_bot_when_present():
    class StopCtx(Ctx):
        def __init__(self):
            super().__init__()
            self.stopped = 0

        async def stop_bot(self):
            self.stopped += 1

    ctx = StopCtx()
    c = _client(ctx)
    res = c.post("/api/bot/stop")
    assert res.status_code == 200
    assert ctx.stopped == 1


def test_bot_stop_falls_back_to_stop_event():
    import asyncio

    ctx = Ctx()
    ctx.stop_event = asyncio.Event()
    c = _client(ctx)
    res = c.post("/api/bot/stop")
    assert res.status_code == 200
    assert ctx.stop_event.is_set()


# -- static page --------------------------------------------------------------

def test_index_serves_html():
    ctx = Ctx()
    c = _client(ctx)
    res = c.get("/")
    assert res.status_code == 200
    assert "Bot arbitrage" in res.text


# -- seuil d'inefficience global (mode chase, 2026-09-23) --------------------------------------

class TwoMarketCtx(Ctx):
    def __init__(self):
        super().__init__()
        self.orch.engines["ETH"] = Engine()
        self.markets = [self.orch.engines["BTC"].cfg, self.orch.engines["ETH"].cfg]


def test_min_edge_endpoint_applies_to_all_markets():
    ctx = TwoMarketCtx()
    c = _client(ctx)
    res = c.post("/api/params/min_edge", json={"bps": 7.5})
    assert res.status_code == 200 and res.json()["min_edge_bps"] == 7.5
    assert all(e.cfg.min_edge_bps == 7.5 for e in ctx.orch.engines.values())


def test_min_edge_endpoint_before_start_updates_config():
    ctx = TwoMarketCtx()
    ctx.orch = None
    c = _client(ctx)
    assert c.post("/api/params/min_edge", json={"bps": 12}).status_code == 200
    assert all(m.min_edge_bps == 12 for m in ctx.markets)
    assert c.get("/api/status").json()["min_edge_bps"] == 12


@pytest.mark.parametrize("body", [{"bps": 0.9}, {"bps": 20.5}, {"bps": -1}, {"bps": "abc"}, {}, {"bps": None},
                                  {"bps": "nan"}])
def test_min_edge_endpoint_rejects_out_of_range(body):
    ctx = TwoMarketCtx()
    c = _client(ctx)
    assert c.post("/api/params/min_edge", json=body).status_code == 400
    assert all(e.cfg.min_edge_bps == 5.0 for e in ctx.orch.engines.values())


def test_min_edge_endpoint_requires_auth():
    ctx = TwoMarketCtx()
    c = TestClient(create_app(ctx), base_url="http://127.0.0.1")
    assert c.post("/api/params/min_edge", json={"bps": 8}).status_code == 400
    c2 = TestClient(create_app(ctx), base_url="http://127.0.0.1", headers={"x-auth-token": "bad"})
    assert c2.post("/api/params/min_edge", json={"bps": 8}).status_code == 400
    assert all(e.cfg.min_edge_bps == 5.0 for e in ctx.orch.engines.values())


# -- animation d'arrêt ------------------------------------------------------------------------

def test_status_exposes_stopping_step():
    ctx = Ctx()
    c = _client(ctx)
    assert c.get("/api/status").json()["stopping"] is None
    ctx.stop_step = "Attente des couvertures en cours"
    assert c.get("/api/status").json()["stopping"] == "Attente des couvertures en cours"


def test_kill_clears_stopping_step_when_done():
    ctx = Ctx()
    ctx.stop_step = None

    async def kill(reason):
        ctx.orch.killed = True
        ctx.stop_step = "Fermeture des positions Var"

    ctx.orch.kill = kill
    c = _client(ctx)
    assert c.post("/api/bot/kill").status_code == 200
    assert ctx.stop_step is None


# -- mode paire pétrole (tâche 4) ---------------------------------------------

from arb.config import PairCfg  # noqa: E402
from arb.pair_orchestrator import PairOrchestrator  # noqa: E402


class FakePair(PairOrchestrator):
    """Orchestrateur de paire minimal : seuls cfg, status() et resume() servent au serveur."""

    def __init__(self):
        self.cfg = PairCfg()
        self.resumed = 0
        self.state = "BLOQUÉ"

    def status(self):
        return {"markets": [], "killed": False, "daily_pnl": -3.0,
                "pair": {"state": self.state, "reason": "test", "cycles": 2}}

    def resume(self):
        self.resumed += 1


class PairCtx(Ctx):
    def __init__(self):
        super().__init__()
        self.orch = FakePair()



def test_pair_status_returns_pair_block():
    c = _client(PairCtx())
    body = c.get("/api/status").json()
    assert body["pair"]["cycles"] == 2 and body["daily_pnl"] == -3.0
    assert body["pair"]["params"] == {"leg_usd": 5000.0, "cycle_min": 10.0, "daily_loss_usd": 200.0, "volume_cycle_min": 0.0}


def test_pair_params_requires_token():
    ctx = PairCtx()
    c = TestClient(create_app(ctx), base_url="http://127.0.0.1")
    assert c.post("/api/pair/params", json={"leg_usd": 7500}).status_code in (400, 401)
    assert c.post("/api/pair/resume").status_code in (400, 401)
    assert ctx.orch.cfg.leg_usd == 5000.0 and ctx.orch.resumed == 0


def test_pair_params_applies_valid_values():
    ctx = PairCtx()
    r = _client(ctx).post("/api/pair/params", json={"leg_usd": 7500, "cycle_min": 5, "daily_loss_usd": 150})
    assert r.status_code == 200
    assert (ctx.orch.cfg.leg_usd, ctx.orch.cfg.cycle_min, ctx.orch.cfg.daily_loss_usd) == (7500.0, 5.0, 150.0)


def test_pair_params_partial_update():
    ctx = PairCtx()
    assert _client(ctx).post("/api/pair/params", json={"cycle_min": 20}).status_code == 200
    assert ctx.orch.cfg.cycle_min == 20.0 and ctx.orch.cfg.leg_usd == 5000.0


@pytest.mark.parametrize("payload", [
    {"leg_usd": 999}, {"leg_usd": 10001}, {"leg_usd": 3000},  # 3000 n'est pas multiple de 2500
    {"cycle_min": 0.9}, {"cycle_min": 61}, {"daily_loss_usd": 19}, {"daily_loss_usd": 1001},
    {"leg_usd": "abc"}, {"cycle_min": None}, {"leg_usd": True},
    {"leg_usd": 5000, "cycle_min": 100},  # un champ invalide annule tout
])
def test_pair_params_out_of_bounds_is_400_and_cfg_unchanged(payload):
    ctx = PairCtx()
    r = _client(ctx).post("/api/pair/params", json=payload)
    assert r.status_code == 400
    assert ctx.orch.cfg == PairCfg()


def test_pair_resume_calls_resume():
    ctx = PairCtx()
    assert _client(ctx).post("/api/pair/resume").status_code == 200
    assert ctx.orch.resumed == 1


def test_pair_endpoints_409_without_pair_orchestrator():
    c = _client(Ctx())  # orchestrateur classique
    assert c.post("/api/pair/params", json={"cycle_min": 5}).status_code == 409
    assert c.post("/api/pair/resume").status_code == 409


def test_startup_bridge_error_is_reported_as_readable_400():
    ctx = Ctx()

    async def failed_start(account):
        raise VarError("Onglet Omni fermé : reconnecter la session")

    ctx.start_bot = failed_start
    response = _client(ctx).post("/api/bot/start", json={"account_address": "0x" + "ab" * 20})
    assert response.status_code == 400
    assert "Onglet Omni fermé" in response.json()["detail"]
