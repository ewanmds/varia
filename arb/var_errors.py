"""Exceptions Var partagées par le client et le limiteur (évite un import circulaire)."""
from __future__ import annotations


class VarError(Exception):
    pass


class VarRateLimited(VarError):
    def __init__(self, wait_s: float) -> None:
        super().__init__(f"rate limited {wait_s}s")
        self.wait_s = wait_s


class VarBudgetExceeded(VarRateLimited):
    """L5 : levée par le limiteur LOCAL quand l'attente nécessaire dépasse l'échéance fournie.
    Ce n'est pas un refus de Var (aucune requête n'est partie, aucun strike)."""


class VarRejected(VarError):
    def __init__(self, status: int, body) -> None:
        super().__init__(f"HTTP {status}: {body}")
        self.status = status
        self.body = body
