"""Servico do STATUS de aceite legal em 2 camadas (plataforma + tenant).

Camada PLATAFORMA: aceite 1x (tenant_id NULL), re-aceite se LEGAL_VERSION mudar.
Camada TENANT (Modelo B): aceite no primeiro acesso a cada estabelecimento; considera
a VERSAO VIGENTE dos docs do tenant (custom ativo OU modelo base). pending_types por
papel: tutor -> service_terms + service_cancellation; passeador -> walker_agreement.

Este modulo NAO faz enforcement (ver app/dependencies/legal_gate.py) nem I/O HTTP; e
consumido pelas rotas (GET /legal/status, POST /legal/acceptance) e pelo gate.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.legal_acceptance import LegalAcceptance
from app.models.tenant import Tenant
from app.models.user import User
from app.services import legal_base_documents as base
from app.services import tenant_legal_document_service as tld


def normalize_role(role: str | None) -> str:
    raw = (role or "").strip().lower()
    if raw in {"walker", "passeador"}:
        return "passeador"
    if raw in {"admin", "super_admin"}:
        return raw
    return "tutor"


# --- Camada PLATAFORMA -------------------------------------------------------
def _platform_acceptance(db: Session, user_id: str, role: str) -> LegalAcceptance | None:
    return (
        db.query(LegalAcceptance)
        .filter(
            LegalAcceptance.user_id == user_id,
            LegalAcceptance.user_role == role,
            LegalAcceptance.tenant_id.is_(None),
        )
        .order_by(LegalAcceptance.accepted_at.desc())
        .first()
    )


def platform_status(db: Session, user: User, legal_version: str) -> dict:
    """{accepted, pending_types} da camada plataforma para o papel do usuario."""
    role = normalize_role(getattr(user, "role", None))
    acc = _platform_acceptance(db, user.id, role)
    version_fields = (
        "terms_version",
        "privacy_version",
        "cancellation_version",
        "lgpd_version",
        "geolocation_version",
    )
    if acc is None:
        return {"accepted": False, "pending_types": list(version_fields)}
    pending = [f for f in version_fields if getattr(acc, f, "") != legal_version]
    return {"accepted": not pending, "pending_types": pending}


# --- Camada TENANT -----------------------------------------------------------
def _tenant_acceptance(db: Session, user_id: str, role: str, tenant_id: str) -> LegalAcceptance | None:
    return (
        db.query(LegalAcceptance)
        .filter(
            LegalAcceptance.user_id == user_id,
            LegalAcceptance.user_role == role,
            LegalAcceptance.tenant_id == tenant_id,
        )
        .order_by(LegalAcceptance.accepted_at.desc())
        .first()
    )


def _tenant_doc_column(doc_type: str) -> str:
    """Coluna de versao usada para persistir o aceite de cada doc_type do tenant."""
    return {
        "service_terms": "terms_version",
        "service_cancellation": "cancellation_version",
        "walker_agreement": "terms_version",
    }[doc_type]


def tenant_pending_types(db: Session, user: User, tenant_id: str) -> list[str]:
    """doc_types do tenant aplicaveis ao papel cuja versao vigente != versao aceita."""
    role = normalize_role(getattr(user, "role", None))
    doc_types = base.doc_types_for_role(role)
    if not doc_types:
        return []
    acc = _tenant_acceptance(db, user.id, role, tenant_id)
    pending: list[str] = []
    for doc_type in doc_types:
        current = tld.effective_version(db, tenant_id, doc_type)
        accepted_version = getattr(acc, _tenant_doc_column(doc_type), None) if acc else None
        if accepted_version != current:
            pending.append(doc_type)
    return pending


def tenant_status(db: Session, user: User, tenant_id: str | None) -> dict | None:
    """Camada tenant: None quando nao ha tenant ativo/vinculo."""
    if not tenant_id:
        return None
    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        return None
    pending = tenant_pending_types(db, user, tenant_id)
    return {
        "tenant_id": tenant_id,
        "tenant_name": getattr(tenant, "name", None),
        "accepted": not pending,
        "pending_types": pending,
    }
