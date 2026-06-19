"""Abstrato de provedor de background check — Fase 0.

Define a interface que cada provedor (manual, flagcheck, idwall, serpro, ...)
deve implementar. A camada de rotas fala apenas com esta interface — nao importa
diretamente o background_check_service.

Fluxo MANUAL (unico funcional agora):
  1. Passeador da consentimento   -> register_consent(profile, version, db)
  2. Passeador envia certidao     -> submit_certificate(profile, payload, db)
  3. Admin valida certidao        -> Admin PATCH (nao passa pelo provider; e UI direto)
  4. Passeador/app consulta status-> get_background_status(profile, certificates)

Pontos de extensao para provedores AUTOMATICOS futuros:
  - start_check(profile, db): dispara verificacao automatica na API do provedor.
  - handle_webhook(payload, db): processa retorno assincrono do provedor.

Ambos levantam NotImplementedError no BaseBackgroundCheckProvider — provedores
pagos os implementam; o manual nao precisa (usa upload manual + admin).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sqlalchemy.orm import Session
    from app.models.walker_profile import WalkerProfile
    from app.models.walker_background_certificate import WalkerBackgroundCertificate


class BackgroundCheckProvider(ABC):
    """Interface publica do provedor de background check por tenant.

    Cada provedor deve:
    - Definir um ``id`` unico (string lowercase, sem espacos).
    - Implementar ``is_configured(tenant)`` — retorna True quando o tenant tem
      credenciais validas para este provedor.
    - Implementar os metodos do fluxo principal (consent, certificate, status).

    Metodos de extensao automatica (start_check / handle_webhook) sao opcionais:
    levantam NotImplementedError no base; provedores pagos os sobrescrevem.
    """

    #: Identificador do provedor. Deve ser unico e estavel (e armazenado no banco).
    id: str

    @abstractmethod
    def is_configured(self, tenant: Any) -> bool:
        """Retorna True se o tenant tem configuracao valida para este provedor.

        Para o provedor manual sempre True.
        Para provedores pagos, verifica se as credenciais estao presentes.
        """

    # ---------------------------------------------------------------------- #
    # Fluxo principal — espelha as 3 rotas do passeador                       #
    # ---------------------------------------------------------------------- #

    @abstractmethod
    def register_consent(
        self,
        profile: "WalkerProfile",
        version: str,
        db: "Session",
    ) -> dict[str, Any]:
        """Registra consentimento LGPD do passeador.

        Retorna dict com ``consent_at`` e ``consent_version``.
        """

    @abstractmethod
    def submit_certificate(
        self,
        profile: "WalkerProfile",
        payload: Any,
        db: "Session",
    ) -> dict[str, Any]:
        """Recebe e persiste uma certidao enviada pelo passeador.

        ``payload`` e o objeto BackgroundCertificatePayload da rota.
        Retorna dict com ``certificate`` (serializado) e ``background_check_status``.
        """

    @abstractmethod
    def get_background_status(
        self,
        profile: "WalkerProfile",
        certificates: list["WalkerBackgroundCertificate"],
    ) -> dict[str, Any]:
        """Retorna o status agregado + lista de certidoes do passeador.

        Retorna dict com:
            background_check_status, background_verified_at,
            consent_at, consent_version, certificates.
        """

    # ---------------------------------------------------------------------- #
    # Extensao para provedores automaticos (futuro)                            #
    # ---------------------------------------------------------------------- #

    def start_check(self, profile: "WalkerProfile", db: "Session") -> dict[str, Any]:
        """Dispara verificacao automatica na API do provedor externo.

        Levanta NotImplementedError no base.
        Provedores pagos (flagcheck/idwall/serpro) devem sobrescrever.
        """
        raise NotImplementedError(
            f"Provedor '{self.id}' nao suporta verificacao automatica (start_check). "
            "Use o fluxo manual ou configure um provedor automatico."
        )

    def handle_webhook(self, payload: dict[str, Any], db: "Session") -> dict[str, Any]:
        """Processa retorno assincrono (webhook) do provedor externo.

        Levanta NotImplementedError no base.
        Provedores pagos (flagcheck/idwall/serpro) devem sobrescrever.
        """
        raise NotImplementedError(
            f"Provedor '{self.id}' nao suporta webhook (handle_webhook). "
            "Use o fluxo manual ou configure um provedor automatico."
        )
