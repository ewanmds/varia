"""Ancien contrôle CDP du mécanisme d'exécution en mode volume, ordres simulés.

Ce test accéléré ne valide pas le signal de tendance.
"""
from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from arb.config import load_config, load_pair_config
from arb.journal import Journal
from arb.pair_orchestrator import PairOrchestrator
from arb.pair_paper import PaperPairVar
from arb.var_client import CdpTransport, VarClient
from arb.var_limiter import VarLimiter


async def check():
    g, _ = load_config(ROOT / "config.toml")
    if g.var_transport != "cdp":
        raise SystemExit("Contrôle réservé au transport CDP. Avec l'extension, utiliser start-paper.cmd pour simuler.")
    # Copie uniquement : config.toml et le serveur gardent des cycles de 10 minutes.
    cfg = replace(load_pair_config(ROOT / "config.toml"), cycle_min=0.1)
    transport = CdpTransport(g.var_cdp_port)
    orch = None
    task = None
    stop = asyncio.Event()
    try:
        await transport.connect()
        var = VarClient(transport, VarLimiter(g.var_min_interval_s, g.strike_window_s, g.strike_max))
        if not await var.health():
            raise RuntimeError("Session Omni non connectée")
        journal = Journal(ROOT / "data" / "journal-preflight-paper.db")
        orch = PairOrchestrator(cfg, g, PaperPairVar(var, cfg), journal)
        await orch.setup()
        task = asyncio.create_task(orch.run(stop, ""))
        for _ in range(60):
            await asyncio.sleep(1)
            if task.done():
                await task
                raise RuntimeError("Boucle de simulation terminée prématurément")
            if orch.status()["pair"]["cycles"] >= 2:
                break
        p = orch.status()["pair"]
        report = {k: p.get(k) for k in ("state", "reason", "cycles", "volume_session_usd", "pos_usd", "ready")}
        report["paper"] = True
        report["test_cycle_seconds"] = 6
        report["closed"] = await orch.close_for_stop(30)
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
        (ROOT / "data" / "preflight-paper-result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        if not report["closed"] or report["cycles"] < 2:
            raise RuntimeError("Contrôle incomplet : voir le rapport")
    finally:
        stop.set()
        if task:
            await task
        await transport.close()


if __name__ == "__main__":
    asyncio.run(check())
