"""Servico dos documentos legais do TENANT (camada por-estabelecimento).

Le/grava TenantLegalDocument. Quando o tenant NUNCA customizou um doc_type, cai no
MODELO BASE (legal_base_documents). Versionamento simples por is_active: no maximo
UMA versao ativa por (tenant_id, doc_type).
"""
from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.legal_acceptance import TenantLegalDocument
from app.services import legal_base_documents as base


def get_active_custom(db: Session, tenant_id: str, doc_type: str) -> TenantLegalDocument | None:
    """Doc custom ATIVO do tenant para o doc_type, se existir."""
    stmt = (
        select(TenantLegalDocument)
        .where(
            TenantLegalDocument.tenant_id == tenant_id,
            TenantLegalDocument.doc_type == doc_type,
            TenantLegalDocument.is_active.is_(True),
        )
        .order_by(TenantLegalDocument.updated_at.desc())
    )
    return db.execute(stmt).scalars().first()


def effective_document(db: Session, tenant_id: str, doc_type: str, tenant_name: str | None) -> dict:
    """Documento VIGENTE (custom ativo OU modelo base) no shape do contrato admin."""
    custom = get_active_custom(db, tenant_id, doc_type)
    if custom is not None:
        return {
            "doc_type": custom.doc_type,
            "title": custom.title,
            "content": custom.content,
            "version": custom.version,
            "updated_at": custom.updated_at,
            "is_custom": True,
        }
    doc = base.base_document(doc_type, tenant_name)
    doc["updated_at"] = None
    return doc


def effective_version(db: Session, tenant_id: str, doc_type: str) -> str:
    """Versao VIGENTE de um doc_type (custom ativo OU base). Usada no aceite/status."""
    custom = get_active_custom(db, tenant_id, doc_type)
    return custom.version if custom is not None else base.BASE_VERSION


def _next_version(db: Session, tenant_id: str, doc_type: str) -> str:
    """Proxima versao custom (v1, v2, ...). Conta versoes ja existentes do par."""
    stmt = select(TenantLegalDocument).where(
        TenantLegalDocument.tenant_id == tenant_id,
        TenantLegalDocument.doc_type == doc_type,
    )
    count = len(db.execute(stmt).scalars().all())
    return f"v{count + 1}"


def upsert_custom(db: Session, tenant_id: str, doc_type: str, title: str, content: str) -> TenantLegalDocument:
    """Grava NOVA versao custom e desativa a anterior (historico preservado)."""
    now = datetime.utcnow()
    # Desativa qualquer versao ativa anterior do par.
    for row in db.execute(
        select(TenantLegalDocument).where(
            TenantLegalDocument.tenant_id == tenant_id,
            TenantLegalDocument.doc_type == doc_type,
            TenantLegalDocument.is_active.is_(True),
        )
    ).scalars().all():
        row.is_active = False
        row.updated_at = now

    doc = TenantLegalDocument(
        id=str(uuid4()),
        tenant_id=tenant_id,
        doc_type=doc_type,
        title=title,
        content=content,
        version=_next_version(db, tenant_id, doc_type),
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    db.add(doc)
    db.flush()
    return doc


def restore_base(db: Session, tenant_id: str, doc_type: str) -> None:
    """Restaura o MODELO BASE: desativa toda versao custom ativa do par."""
    now = datetime.utcnow()
    for row in db.execute(
        select(TenantLegalDocument).where(
            TenantLegalDocument.tenant_id == tenant_id,
            TenantLegalDocument.doc_type == doc_type,
            TenantLegalDocument.is_active.is_(True),
        )
    ).scalars().all():
        row.is_active = False
        row.updated_at = now
