"""Feature flags controladas por variável de ambiente.

Uso:
    from app.core.feature_flags import MULTI_TENANT_WALKER

    if MULTI_TENANT_WALKER:
        ...  # comportamento novo

Padrão: cada flag é avaliada UMA VEZ no import (processo inteiro).
Para testes que precisam mudar o valor, use monkeypatch na variável de módulo.

Flag OFF (default) = no-op / comportamento idêntico ao atual.
"""
import os


def _flag(name: str, default: bool = False) -> bool:
    """Lê uma env var como booleano.

    Valores truthy: "true", "1", "yes", "on" (case-insensitive).
    Qualquer outro valor (incluindo ausente) = False quando default=False.
    """
    return os.getenv(name, "true" if default else "false").lower() in {"true", "1", "yes", "on"}


# ── Flags ─────────────────────────────────────────────────────────────────────

# Fase 1 — Passeador Multi-Tenant.
# OFF por default: comportamento idêntico ao atual (zero-regressão).
# Ligar: MULTI_TENANT_WALKER=true (ou 1/yes/on).
MULTI_TENANT_WALKER: bool = _flag("MULTI_TENANT_WALKER", False)


def multi_tenant_walker_enabled() -> bool:
    """Relê a env var a cada chamada (sem cache de módulo).

    Permite monkeypatch via monkeypatch.setenv("MULTI_TENANT_WALKER", "true")
    em testes sem precisar reimportar o módulo.
    """
    return _flag("MULTI_TENANT_WALKER", False)
