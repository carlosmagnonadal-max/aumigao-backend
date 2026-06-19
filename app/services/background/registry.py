"""Registry de provedores de background check por tenant.

Uso:
    provider = get_background_provider(db, tenant_id)
    result = provider.register_consent(profile, version, db)

Provedores disponiveis:
    "manual"    — Fase 0: certidoes offline + validacao admin. FUNCIONAL.
    "flagcheck" — Slot reservado (nao configurado em nenhum tenant ainda).
    "idwall"    — Slot reservado (nao configurado em nenhum tenant ainda).
    "serpro"    — Slot reservado (nao configurado em nenhum tenant ainda).

Qualquer tenant sem setting ou com setting="manual" recebe o ManualProvider.
Tenants configurados com um provedor pago recebem um PlaceholderProvider que
levanta erro claro ao ser chamado — nenhum tenant esta nessa situacao hoje,
portanto ZERO regressao.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import HTTPException

from app.services.background.base import BackgroundCheckProvider
from app.services.background.manual import ManualProvider

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

# Valores validos para background_check_provider.
VALID_PROVIDERS = ("manual", "flagcheck", "idwall", "serpro")

# Singleton do provedor manual (sem estado; pode ser compartilhado).
_MANUAL_PROVIDER = ManualProvider()


class _PlaceholderProvider(BackgroundCheckProvider):
    """Provedor pago ainda nao integrado — levanta erro claro ao ser chamado.

    Garante que, se algum tenant for acidentalmente configurado para um provedor
    pago antes da integracao estar pronta, a falha seja explicita (nao silenciosa).
    """

    def __init__(self, provider_id: str) -> None:
        self.id = provider_id

    def is_configured(self, tenant: Any) -> bool:
        return False

    def _not_configured(self) -> None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Provedor de background check '{self.id}' nao esta configurado "
                "para este tenant. Entre em contato com o suporte da plataforma."
            ),
        )

    def register_consent(self, profile: Any, version: str, db: Any) -> dict[str, Any]:
        self._not_configured()

    def submit_certificate(self, profile: Any, payload: Any, db: Any) -> dict[str, Any]:
        self._not_configured()

    def get_background_status(self, profile: Any, certificates: list[Any]) -> dict[str, Any]:
        self._not_configured()


def get_background_provider(
    db: "Session",
    tenant_id: str | None,
) -> BackgroundCheckProvider:
    """Retorna o provedor correto para o tenant.

    Algoritmo:
    1. Busca TenantSettings do tenant.
    2. Le background_check_provider (default "manual" se ausente/None).
    3. "manual" -> ManualProvider (singleton).
    4. Provedor pago reconhecido -> PlaceholderProvider (erro claro ao chamar).
    5. Valor desconhecido -> fallback ManualProvider (fail-open seguro).

    Com flag `background_checks` OFF (default), as rotas nem chegam a chamar o
    provider — o provider layer e transparente.
    """
    from app.models.tenant import TenantSettings

    provider_id = "manual"

    if tenant_id:
        settings = (
            db.query(TenantSettings)
            .filter(TenantSettings.tenant_id == tenant_id)
            .first()
        )
        if settings and settings.background_check_provider:
            provider_id = settings.background_check_provider.strip().lower()

    if provider_id in ("manual", "") or provider_id not in VALID_PROVIDERS:
        return _MANUAL_PROVIDER

    # Provedor pago reconhecido, mas sem integracao real ainda.
    return _PlaceholderProvider(provider_id)
