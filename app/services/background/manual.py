"""Provedor manual de background check — Fase 0 (unico funcional).

Delega integralmente ao ``app.services.background_check_service`` e ao fluxo
existente de WalkerBackgroundCertificate. Comportamento IDENTICO ao anterior:
zero regressao quando provider="manual" (default).

O admin valida certidoes diretamente via PATCH /admin/.../background-certificate/{id}
(nao passa pelo provider — e uma acao de UI do admin, nao do passeador).
"""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from fastapi import HTTPException

from app.services.background.base import BackgroundCheckProvider
from app.services.background_check_service import (
    compute_background_status,
    official_validation_url as background_check_official_url,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session
    from app.models.walker_profile import WalkerProfile
    from app.models.walker_background_certificate import WalkerBackgroundCertificate


# Tipos de certidao aceitos (espelho de walker.py).
_BACKGROUND_CERT_TYPES = {"pf", "tj", "trf", "tse"}
_DEFAULT_CONSENT_VERSION = "v1"


def _serialize_cert(cert: "WalkerBackgroundCertificate") -> dict[str, Any]:
    return {
        "id": cert.id,
        "cert_type": cert.cert_type,
        "issuer_uf": cert.issuer_uf,
        "cert_number": cert.cert_number,
        "document_url": cert.document_url,
        "status": cert.status,
        "validated_at": cert.validated_at,
        "expires_at": cert.expires_at,
        "official_validation_url": background_check_official_url(cert.cert_type, cert.issuer_uf, cert.cert_number),
        "created_at": cert.created_at,
        "updated_at": cert.updated_at,
    }


class ManualProvider(BackgroundCheckProvider):
    """Provedor manual: passeador envia certidoes offline, admin valida.

    is_configured() sempre True — nao requer credenciais.
    Todos os metodos delegam ao servico existente (background_check_service).
    """

    id = "manual"

    def is_configured(self, tenant: Any) -> bool:
        return True

    def register_consent(
        self,
        profile: "WalkerProfile",
        version: str,
        db: "Session",
    ) -> dict[str, Any]:
        version = version or _DEFAULT_CONSENT_VERSION
        profile.background_consent_at = datetime.utcnow()
        profile.background_consent_version = version
        profile.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(profile)
        return {
            "consent_at": profile.background_consent_at,
            "consent_version": profile.background_consent_version,
        }

    def submit_certificate(
        self,
        profile: "WalkerProfile",
        payload: Any,
        db: "Session",
    ) -> dict[str, Any]:
        from app.models.walker_background_certificate import WalkerBackgroundCertificate

        if not profile.background_consent_at:
            raise HTTPException(
                status_code=400,
                detail="Consentimento de antecedentes obrigatorio antes de enviar certidoes.",
            )

        cert_type = (payload.cert_type or "").strip().lower()
        if cert_type not in _BACKGROUND_CERT_TYPES:
            raise HTTPException(status_code=400, detail="Tipo de certidao invalido.")

        uf = (payload.uf or "").strip().upper() or None
        cert_number = (payload.cert_number or "").strip()
        if not cert_number:
            raise HTTPException(status_code=400, detail="Numero da certidao obrigatorio.")

        # 1 linha por cert_type — cria ou atualiza; reenvio volta a "pending".
        existing = (
            db.query(WalkerBackgroundCertificate)
            .filter(
                WalkerBackgroundCertificate.walker_profile_id == profile.id,
                WalkerBackgroundCertificate.cert_type == cert_type,
            )
            .first()
        )
        if existing:
            cert = existing
            cert.issuer_uf = uf
            cert.cert_number = cert_number
            cert.document_url = payload.document_url
            cert.status = "pending"
            cert.validated_by_admin_id = None
            cert.validated_at = None
            cert.updated_at = datetime.utcnow()
        else:
            cert = WalkerBackgroundCertificate(
                id=str(uuid4()),
                walker_profile_id=profile.id,
                cert_type=cert_type,
                issuer_uf=uf,
                cert_number=cert_number,
                document_url=payload.document_url,
                status="pending",
            )
            db.add(cert)
        db.flush()

        certificates = (
            db.query(WalkerBackgroundCertificate)
            .filter(WalkerBackgroundCertificate.walker_profile_id == profile.id)
            .all()
        )
        aggregate = compute_background_status(profile, certificates)
        db.commit()
        db.refresh(cert)
        return {
            "certificate": _serialize_cert(cert),
            "background_check_status": aggregate,
        }

    def get_background_status(
        self,
        profile: "WalkerProfile",
        certificates: list["WalkerBackgroundCertificate"],
    ) -> dict[str, Any]:
        aggregate = compute_background_status(profile, certificates)
        return {
            "background_check_status": aggregate,
            "background_verified_at": profile.background_verified_at,
            "consent_at": profile.background_consent_at,
            "consent_version": profile.background_consent_version,
            "certificates": [_serialize_cert(c) for c in certificates],
        }
