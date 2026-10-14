"""Garde-fou : aucun test n'ouvre de connexion réseau vers le websocket de prix Var.

L'orchestrateur lance VarPriceStream.run() dans run() (L2) : sans connecteur injecté, on refuse
la connexion au lieu de joindre le vrai serveur (la boucle de reconnexion l'absorbe)."""
import pytest

from arb import var_ws


@pytest.fixture(autouse=True)
def _no_var_ws_network(monkeypatch):
    async def _open(self):
        if self._connect is not None:
            return await self._connect(self.url)
        raise ConnectionError("réseau interdit en test")

    monkeypatch.setattr(var_ws.VarPriceStream, "_open", _open)
