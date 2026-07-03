"""product_highlights.py — Vitrine de Destaques e Promoções do tenant (Fase 1).

Diferencial do plano ENTERPRISE. Curadoria de POUCOS produtos/serviços em destaque/
promoção que o tenant mostra no app do tutor (demonstração, SEM transação nesta fase).

Rotas ADMIN (CRUD do catálogo curado):
  - GET    /admin/product-highlights          — lista do tenant do escopo (inclui inativos)
  - POST   /admin/product-highlights          — cria
  - PATCH  /admin/product-highlights/{id}      — atualiza (parcial)
  - DELETE /admin/product-highlights/{id}      — remove

Rota APP DO TUTOR:
  - GET    /api/product-highlights            — só ATIVOS do tenant da request, sanitizados

GATING (3 camadas, padrão do Perfil Vivo, TODAS default-OFF):
  1. env PRODUCT_HIGHLIGHTS_ENABLED (master switch);
  2. toggle por tenant `product_highlights` (TenantFeature, default OFF);
  3. plano ENTERPRISE-only (enforce_enterprise_only → 403 teaser plan_upgrade_required
     required_plan="enterprise"). Pro/free com toggle ligado = 403 de upgrade.
  Dormente (env off OU toggle off) → 404 (não vaza existência do recurso).

REGRA DE OURO do repo: todo endpoint de ESCRITA admin chama get_admin_tenant_scope no
topo (injeta o GUC RLS antes de qualquer INSERT/UPDATE — bug recorrente de RLS scope).
"""
from __future__ import annotations

import os

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.dependencies.auth import get_current_user, require_admin
from app.dependencies.tenant_scope import get_admin_tenant_scope
from app.models.tenant import Tenant
from app.models.user import User
from app.services import product_highlight_service as svc
from app.services.tenant_free_plan_service import enforce_enterprise_only
from app.services.tenant_plan_service import tenant_feature_enabled

FEATURE_KEY = "product_highlights"
FEATURE_LABEL = "Vitrine de destaques é um recurso do plano Enterprise."

router = APIRouter(prefix="/admin/product-highlights", tags=["product-highlights-admin"])
api_router = APIRouter(prefix="/api/admin/product-highlights", tags=["product-highlights-admin"])

# App do tutor (tenant resolvido da request, sem prefixo /admin).
tutor_router = APIRouter(prefix="/product-highlights", tags=["product-highlights"])
api_tutor_router = APIRouter(prefix="/api/product-highlights", tags=["product-highlights"])


# ---------------------------------------------------------------------------
# Gate (3 camadas)
# ---------------------------------------------------------------------------

def _env_on() -> bool:
    return os.getenv("PRODUCT_HIGHLIGHTS_ENABLED", "false").lower() in {"1", "true", "yes", "on"}


def _require_vitrine(db: Session, tenant: Tenant | None) -> None:
    """Env master + toggle do tenant (dormente = 404) + plano Enterprise (403 teaser)."""
    if not _env_on():
        raise HTTPException(status_code=404, detail="Not found")
    if tenant is None or not tenant_feature_enabled(tenant, db, FEATURE_KEY):
        raise HTTPException(status_code=404, detail="Not found")
    enforce_enterprise_only(tenant, db, feature=FEATURE_KEY, label=FEATURE_LABEL)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class HighlightCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=svc.TITLE_MAX)
    description: str | None = Field(None, max_length=svc.DESCRIPTION_MAX)
    photo_url: str | None = Field(None, max_length=svc.PHOTO_URL_MAX)
    price_cents: int | None = Field(None, ge=0)
    promo_price_cents: int | None = Field(None, ge=0)
    is_active: bool = True
    sort_order: int = 0


class HighlightUpdate(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=svc.TITLE_MAX)
    description: str | None = Field(None, max_length=svc.DESCRIPTION_MAX)
    photo_url: str | None = Field(None, max_length=svc.PHOTO_URL_MAX)
    price_cents: int | None = Field(None, ge=0)
    promo_price_cents: int | None = Field(None, ge=0)
    is_active: bool | None = None
    sort_order: int | None = None


# ---------------------------------------------------------------------------
# Helpers admin
# ---------------------------------------------------------------------------

def _admin_tenant(admin: User, db: Session) -> Tenant:
    """Escopo do admin (injeta GUC RLS) + resolve o Tenant do escopo.

    super_admin sem act-as (escopo global) NÃO tem um tenant único — nesse caso
    exigimos que ele opere como tenant (act-as) para gerenciar a vitrine, que é
    inerentemente por-tenant. Admin de tenant usa o próprio tenant_id.
    """
    scope = get_admin_tenant_scope(admin, db)
    if not scope.tenant_id:
        raise HTTPException(
            status_code=400,
            detail="Selecione um tenant (act-as) para gerenciar a vitrine de destaques.",
        )
    tenant = db.get(Tenant, scope.tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant não encontrado")
    return tenant


# ---------------------------------------------------------------------------
# CRUD admin
# ---------------------------------------------------------------------------

@router.get("")
@api_router.get("")
def admin_list(admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    tenant = _admin_tenant(admin, db)
    _require_vitrine(db, tenant)
    items = svc.list_for_admin(db, tenant.id)
    return {
        "items": [svc.to_admin_dict(h) for h in items],
        "max_active": svc.product_highlights_max_active(),
    }


@router.post("", status_code=201)
@api_router.post("", status_code=201)
def admin_create(payload: HighlightCreate, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    tenant = _admin_tenant(admin, db)
    _require_vitrine(db, tenant)
    highlight = svc.create_highlight(
        db, tenant.id,
        title=payload.title, description=payload.description, photo_url=payload.photo_url,
        price_cents=payload.price_cents, promo_price_cents=payload.promo_price_cents,
        is_active=payload.is_active, sort_order=payload.sort_order,
    )
    db.commit()
    db.refresh(highlight)
    return {"item": svc.to_admin_dict(highlight)}


@router.patch("/{highlight_id}")
@api_router.patch("/{highlight_id}")
def admin_update(highlight_id: str, payload: HighlightUpdate,
                 admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    tenant = _admin_tenant(admin, db)
    _require_vitrine(db, tenant)
    highlight = svc.get_owned(db, tenant.id, highlight_id)
    if not highlight:
        raise HTTPException(status_code=404, detail="Destaque não encontrado")
    fields = payload.model_dump(exclude_unset=True)
    highlight = svc.update_highlight(db, highlight, fields=fields)
    db.commit()
    db.refresh(highlight)
    return {"item": svc.to_admin_dict(highlight)}


@router.delete("/{highlight_id}")
@api_router.delete("/{highlight_id}")
def admin_delete(highlight_id: str, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    tenant = _admin_tenant(admin, db)
    _require_vitrine(db, tenant)
    highlight = svc.get_owned(db, tenant.id, highlight_id)
    if not highlight:
        raise HTTPException(status_code=404, detail="Destaque não encontrado")
    svc.delete_highlight(db, highlight)
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# App do tutor — vitrine pública (só ativos do tenant da request)
# ---------------------------------------------------------------------------

def _tenant_of_request(db: Session, request: Request, user: User) -> Tenant | None:
    """Tenant da request (padrão das rotas do tutor): tenant resolvido no middleware,
    com fallback para o tenant_id do usuário autenticado (BFF nem sempre injeta slug)."""
    tenant_id = getattr(request.state, "tenant_id", None) or getattr(user, "tenant_id", None)
    return db.get(Tenant, tenant_id) if tenant_id else None


@tutor_router.get("")
@api_tutor_router.get("")
def tutor_list(request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    tenant = _tenant_of_request(db, request, user)
    _require_vitrine(db, tenant)
    items = svc.list_active_public(db, tenant.id)
    return {"items": [svc.to_public_dict(h) for h in items]}
