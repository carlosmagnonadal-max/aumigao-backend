"""admin_legal_documents.py — configuracao dos documentos legais do TENANT (Fase 2).

O tenant configura os PROPRIOS documentos a partir de MODELOS BASE fornecidos pela
plataforma SEM responsabilizacao (cada estabelecimento valida com seu advogado).

Rotas ADMIN (par com e sem /api, padrao da casa):
  - GET    /api/admin/legal-documents            — lista os 3 docs vigentes (custom ou base)
  - PUT    /api/admin/legal-documents/{doc_type}  — grava versao custom nova (v1, v2, ...)
  - DELETE /api/admin/legal-documents/{doc_type}  — restaura o modelo base

REGRA DE OURO do repo: todo endpoint de ESCRITA admin chama get_admin_tenant_scope no
topo (injeta o GUC RLS antes de qualquer INSERT/UPDATE — bug recorrente de RLS scope).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.dependencies.auth import require_admin
from app.dependencies.tenant_scope import get_admin_tenant_scope
from app.models.tenant import Tenant
from app.models.user import User
from app.services import legal_base_documents as base
from app.services import tenant_legal_document_service as tld

router = APIRouter(prefix="/admin/legal-documents", tags=["admin-legal-documents"])
api_router = APIRouter(prefix="/api/admin/legal-documents", tags=["admin-legal-documents"])


class LegalDocumentUpdate(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    content: str = Field(..., min_length=1)


def _admin_tenant(admin: User, db: Session) -> Tenant:
    """Escopo do admin (injeta GUC RLS) + resolve o Tenant do escopo."""
    scope = get_admin_tenant_scope(admin, db)
    if not scope.tenant_id:
        raise HTTPException(
            status_code=400,
            detail="Selecione um tenant (act-as) para gerenciar os documentos legais.",
        )
    tenant = db.get(Tenant, scope.tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant nao encontrado")
    return tenant


def _validate_doc_type(doc_type: str) -> None:
    if not base.is_valid_doc_type(doc_type):
        raise HTTPException(status_code=404, detail="Tipo de documento invalido")


@router.get("")
@api_router.get("")
def admin_list_documents(admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    tenant = _admin_tenant(admin, db)
    tenant_name = getattr(tenant, "name", None)
    documents = [
        tld.effective_document(db, tenant.id, doc_type, tenant_name)
        for doc_type in base.ALL_DOC_TYPES
    ]
    return {"documents": documents}


@router.put("/{doc_type}")
@api_router.put("/{doc_type}")
def admin_update_document(
    doc_type: str,
    payload: LegalDocumentUpdate,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    tenant = _admin_tenant(admin, db)
    _validate_doc_type(doc_type)
    doc = tld.upsert_custom(db, tenant.id, doc_type, payload.title, payload.content)
    db.commit()
    db.refresh(doc)
    return {
        "doc_type": doc.doc_type,
        "title": doc.title,
        "content": doc.content,
        "version": doc.version,
        "updated_at": doc.updated_at,
        "is_custom": True,
    }


@router.delete("/{doc_type}")
@api_router.delete("/{doc_type}")
def admin_restore_base_document(
    doc_type: str,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    tenant = _admin_tenant(admin, db)
    _validate_doc_type(doc_type)
    tld.restore_base(db, tenant.id, doc_type)
    db.commit()
    doc = base.base_document(doc_type, getattr(tenant, "name", None))
    doc["updated_at"] = None
    return doc
