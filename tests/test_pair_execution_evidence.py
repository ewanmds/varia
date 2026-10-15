"""Execution evidence is atomic, explicit about unknowns, and never sends requests."""
import sqlite3

import pytest

from arb.config import PairCfg
from arb.journal import Journal, SafeJournal
from arb.pair_exec import PairExecutor
from arb.pricing import VarQuote
from tests.test_pair_exec import FakeVar, J, nosleep


def fill(executor, price, *, reduce_only=False):
    quote = VarQuote(89.99, 90.01, 2, 100, "must-not-be-persisted")
    return executor._fill("wti", "buy", 2, reduce_only, quote, price, 100)


@pytest.mark.parametrize("price,source", [(90.02, "order_status"), (None, "quote_proxy")])
def test_fill_persists_price_provenance_and_unverified_quantity(tmp_path, price, source):
    journal = Journal(tmp_path / "evidence.db")
    executor = PairExecutor(object(), PairCfg(), journal, clock=lambda: 100.25)
    result = fill(executor, price, reduce_only=True)
    legacy = journal.recent_arbs()[0]
    evidence = dict(journal.db.execute("SELECT * FROM pair_execution_evidence").fetchone())
    assert evidence["arb_id"] == legacy["id"] and evidence["ts"] == legacy["ts"]
    assert evidence["price_source"] == source
    assert evidence["qty"] == legacy["var_qty"] == 2
    assert evidence["qty_source"] == "requested_unverified"
    assert evidence["quote_mid"] == 90 and evidence["quote_px"] == 90.01
    assert evidence["half_spread_bps"] == pytest.approx(result.half_spread_bps)
    assert evidence["reduce_only"] == 1
    # Existing accounting values remain unchanged, including its quote fallback.
    assert legacy["var_px"] == result.exec_px == (90.01 if price is None else price)
    assert legacy["edge_bps"] == -result.slip_bps
    assert legacy["pnl_usd"] == 0 and legacy["status"] == "pair_fill"
    if price is None:
        assert evidence["exec_px"] is None and evidence["slip_bps"] is None
        assert result.slip_bps == 0  # legacy estimate is never promoted to evidence
    else:
        assert evidence["exec_px"] == price
        assert evidence["slip_bps"] == pytest.approx(result.slip_bps)
    assert "must-not-be-persisted" not in repr(evidence)


def test_fill_and_evidence_share_one_commit(tmp_path):
    journal = Journal(tmp_path / "atomic.db")
    statements = []
    journal.db.set_trace_callback(statements.append)
    fill(PairExecutor(object(), PairCfg(), journal, clock=lambda: 100.25), 90.02)
    assert sum(sql == "COMMIT" for sql in statements) == 1
    assert len(journal.recent_arbs()) == 1
    assert journal.db.execute("SELECT COUNT(*) FROM pair_execution_evidence").fetchone()[0] == 1


def test_failed_evidence_insert_rolls_back_both_rows_and_does_not_stop_fill(tmp_path, capsys):
    journal = Journal(tmp_path / "rollback.db")
    journal.db.execute("CREATE TRIGGER fail_evidence BEFORE INSERT ON pair_execution_evidence "
                       "BEGIN SELECT RAISE(ABORT, 'evidence unavailable'); END")
    executor = PairExecutor(object(), PairCfg(), journal, clock=lambda: 100.25)
    result = fill(executor, 90.02)
    assert result.exec_px == 90.02
    assert journal.recent_arbs() == []
    assert journal.db.execute("SELECT COUNT(*) FROM pair_execution_evidence").fetchone()[0] == 0
    assert not journal.db.in_transaction
    assert "evidence unavailable" in capsys.readouterr().err


def test_safe_journal_legacy_fallback_preserves_exact_arb_signature():
    journal = J()
    executor = PairExecutor(object(), PairCfg(), journal, clock=lambda: 100.25)
    result = fill(executor, None)
    assert journal.rows == [("USOILP", "", 0.0, 0.0, 0.0, "buy", 90.01, 2,
                             -result.slip_bps, 0.0, 250.0, "pair_fill")]
    assert SafeJournal.wrap(executor.journal) is executor.journal


def test_failed_legacy_journal_never_interrupts_fill(capsys):
    class Broken:
        def record_arb(self, *args):
            raise OSError("disk unavailable")

    result = fill(PairExecutor(object(), PairCfg(), Broken(), clock=lambda: 100.25), None)
    assert result.exec_px == 90.01
    assert "disk unavailable" in capsys.readouterr().err


@pytest.mark.parametrize("price", [None, 90.02])
async def test_evidence_adds_no_network_calls_to_normal_order(tmp_path, price):
    class CountedVar(FakeVar):
        def __init__(self):
            super().__init__()
            self.seen = dict(positions=0, quote=0, market=0, status=0)

        async def positions(self, *args, **kwargs):
            self.seen["positions"] += 1
            return await super().positions(*args, **kwargs)

        async def quote(self, *args, **kwargs):
            self.seen["quote"] += 1
            return await super().quote(*args, **kwargs)

        async def market(self, *args, **kwargs):
            self.seen["market"] += 1
            return await super().market(*args, **kwargs)

        async def order_status(self, *args, **kwargs):
            self.seen["status"] += 1
            return {"status": "cleared", "price": price}

    var = CountedVar()
    journal = Journal(tmp_path / "calls.db")
    executor = PairExecutor(var, PairCfg(order_gap_s=0), journal, sleep=nosleep)
    await executor._once("wti", "buy", 2, False)
    assert var.seen == {"positions": 1, "quote": 1, "market": 1, "status": 1}
    evidence = journal.db.execute("SELECT price_source, qty_source FROM pair_execution_evidence").fetchone()
    assert tuple(evidence) == ("quote_proxy" if price is None else "order_status", "requested_unverified")


def test_evidence_schema_upgrades_existing_database_without_changing_rows(tmp_path):
    path = tmp_path / "existing.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE arbs (id INTEGER PRIMARY KEY, ts REAL, market TEXT, hl_side TEXT, hl_px REAL,"
                " hl_sz REAL, hl_fee REAL, var_side TEXT, var_px REAL, var_qty REAL, edge_bps REAL,"
                " pnl_usd REAL, latency_ms REAL, status TEXT)")
    old.execute("INSERT INTO arbs (id, market, pnl_usd, status) VALUES (1, 'USOILP', 1.5, 'pair_pnl')")
    old.commit()
    old.close()
    journal = Journal(path)
    assert journal.recent_arbs()[0]["pnl_usd"] == 1.5
    assert journal.db.execute("SELECT COUNT(*) FROM pair_execution_evidence").fetchone()[0] == 0
