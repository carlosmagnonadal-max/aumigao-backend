"""walker_indications.py — Indicar passeador (tutor indica um passeador conhecido).

Fluxo:
  1. Tutor autenticado indica um passeador → POST /api/walker-indications.
     - Gated pela feature `client_referrals` do tenant (default-ON).
     - Usa o tenant ATIVO da request (db.info["rls_tenant"]), NUNCA user.tenant_id.
     - Devolve share_url = {SITE_URL}/seja-passeador?ind={id}&t={slug}.

  2. Tutor lista suas próprias indicações → GET /api/walker-indications.

  3. Lead público (sem auth) → POST /api/public/walker-leads.
     - Resolução de tenant: indication_id → tenant_slug → tenant padrão.
     - Rate limit por IP (mesmo mecanismo de candidaturas: 10/10min).
     - Injeção do GUC RLS antes do INSERT (padrão público com escrita).
     - Cria WalkerLead; promove indicação para "lead_criado" se indication_id válido.
     - Notifica admins do tenant.

REGRA DE OURO: todo endpoint de escrita chama set_session_tenant / injeta RLS
antes de qualquer INSERT. Em rotas públicas sem auth, isso é feito manualmente
(espelho de tutor_referrals.py validate-code + global_scope_session).
"""
from __future__ import annotations

import os
from datetime import datetime
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.database import get_db, get_global_db, set_session_tenant
from app.dependencies.auth import get_current_user
from app.models.tenant import Tenant
from app.models.user import User
from app.models.walker_indication import WalkerIndication, WalkerLead
from app.routes.notifications import NotificationCreate, _create_notification
from app.services.tenant_plan_service import tenant_feature_enabled
from app.services.tenant_seed_service import default_tenant_id
from app.services.upload_validation import application_rate_limiter

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

FEATURE_KEY = "client_referrals"

_SITE_PUBLIC_URL_DEFAULT = "https://www.aumigaowalk.com.br"


def _site_public_url() -> str:
    return os.getenv("SITE_PUBLIC_URL", _SITE_PUBLIC_URL_DEFAULT).rstrip("/")


# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/walker-indications", tags=["walker-indications"])
api_router = APIRouter(prefix="/api/walker-indications", tags=["walker-indications"])

public_router = APIRouter(prefix="/public/walker-leads", tags=["walker-leads-public"])
api_public_router = APIRouter(
    prefix="/api/public/walker-leads", tags=["walker-leads-public"]
)

# ---------------------------------------------------------------------------
# Helpers de tenant
# ---------------------------------------------------------------------------


def _tenant_id_from_request(db: Session) -> str:
    """Retorna o tenant ATIVO da request via rls_tenant (injetado por get_db).

    NÃO usa user.tenant_id — padrão documentado em product_highlights.py /
    notifications.py para evitar cross-tenant leak em multi-tenant (Modelo B).
    """
    rls = db.info.get("rls_tenant")
    if rls and rls not in ("*", ""):
        return rls
    return ""


def _require_active_tenant(db: Session) -> Tenant:
    """Resolve o tenant ativo da request e garante que existe. Lança 400 se ausente."""
    tenant_id = _tenant_id_from_request(db)
    if not tenant_id:
        raise HTTPException(status_code=400, detail="Tenant não resolvido na request.")
    tenant = db.get(Tenant, tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant não encontrado.")
    return tenant


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class WalkerIndicationCreate(BaseModel):
    walker_name: str = Field(..., min_length=1, max_length=200)
    walker_phone: str | None = Field(None, max_length=30)
    note: str | None = Field(None, max_length=500)


class WalkerLeadCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    phone: str = Field(..., min_length=1, max_length=30)
    city: str | None = Field(None, max_length=120)
    indication_id: str | None = Field(None, max_length=40)
    tenant_slug: str | None = Field(None, max_length=80)


# ---------------------------------------------------------------------------
# Helpers internos
# ---------------------------------------------------------------------------


def _share_url(indication_id: str, tenant_slug: str | None) -> str:
    base = _site_public_url()
    slug_part = f"&t={tenant_slug}" if tenant_slug else ""
    return f"{base}/seja-passeador?ind={indication_id}{slug_part}"


def _indication_dict(ind: WalkerIndication, tenant_slug: str | None = None) -> dict:
    return {
        "id": ind.id,
        "walker_name": ind.walker_name,
        "walker_phone": ind.walker_phone,
        "status": ind.status,
        "created_at": ind.created_at,
        "share_url": _share_url(ind.id, tenant_slug),
    }


def _notify_admins_new_lead(
    db: Session,
    lead: WalkerLead,
    tenant_id: str,
) -> None:
    """Notifica todos os admins do tenant sobre o novo lead de passeador."""
    admins = (
        db.query(User)
        .filter(
            User.role.in_(["admin", "super_admin"]),
            User.tenant_id == tenant_id,
        )
        .all()
    )
    # Fallback: super_admins globais (sem tenant_id) também recebem.
    if not admins:
        admins = (
            db.query(User)
            .filter(User.role.in_(["super_admin"]))
            .all()
        )
    if not admins:
        return

    city_info = f", cidade: {lead.city}" if lead.city else ""
    body = (
        f"Nome: {lead.name}, telefone: {lead.phone}{city_info}. "
        "Acesse o painel para entrar em contato."
    )

    for admin in admins:
        _create_notification(
            db,
            NotificationCreate(
                tenant_id=tenant_id,
                user_id=admin.id,
                user_role=admin.role,
                title="Novo candidato a passeador indicado",
                message=body,
                type="walker_lead_new",
                related_entity_type="walker_lead",
                related_entity_id=lead.id,
                metadata={
                    "lead_id": lead.id,
                    "name": lead.name,
                    "city": lead.city or "",
                    "indication_id": lead.indication_id or "",
                    "channel": "in_app",
                },
            ),
        )


# ---------------------------------------------------------------------------
# POST /walker-indications  — tutor cria indicação
# ---------------------------------------------------------------------------


def _create_indication(
    payload: WalkerIndicationCreate,
    user: User,
    db: Session,
) -> dict:
    tenant = _require_active_tenant(db)

    if not tenant_feature_enabled(tenant, db, FEATURE_KEY):
        raise HTTPException(
            status_code=403,
            detail="Indicações de passeador não estão habilitadas para este tenant.",
        )

    now = datetime.utcnow()
    indication = WalkerIndication(
        id=str(uuid4()),
        tenant_id=tenant.id,
        tutor_user_id=user.id,
        walker_name=payload.walker_name.strip(),
        walker_phone=(payload.walker_phone or "").strip() or None,
        note=(payload.note or "").strip() or None,
        status="enviada",
        created_at=now,
        updated_at=now,
    )
    db.add(indication)
    db.commit()
    db.refresh(indication)

    return _indication_dict(indication, tenant_slug=getattr(tenant, "slug", None))


@router.post("", status_code=201)
@api_router.post("", status_code=201)
def create_indication(
    payload: WalkerIndicationCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    return _create_indication(payload, user, db)


# ---------------------------------------------------------------------------
# GET /walker-indications  — tutor lista suas indicações
# ---------------------------------------------------------------------------


def _list_indications(user: User, db: Session) -> dict:
    tenant = _require_active_tenant(db)
    slug = getattr(tenant, "slug", None)

    rows = (
        db.query(WalkerIndication)
        .filter(
            WalkerIndication.tenant_id == tenant.id,
            WalkerIndication.tutor_user_id == user.id,
        )
        .order_by(WalkerIndication.created_at.desc())
        .all()
    )

    return {
        "items": [
            {
                "id": r.id,
                "walker_name": r.walker_name,
                "walker_phone": r.walker_phone,
                "status": r.status,
                "created_at": r.created_at,
                "share_url": _share_url(r.id, slug),
            }
            for r in rows
        ]
    }


@router.get("")
@api_router.get("")
def list_indications(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    return _list_indications(user, db)


# ---------------------------------------------------------------------------
# POST /public/walker-leads  — rota pública, sem auth
# ---------------------------------------------------------------------------


def _get_client_ip(request: Request) -> str:
    forwarded = (request.headers.get("X-Forwarded-For") or "").strip()
    return forwarded.split(",")[0].strip() if forwarded else (
        request.client.host if request.client else "unknown"
    )


def _create_lead(
    payload: WalkerLeadCreate,
    request: Request,
    db: Session,
) -> dict:
    """Cria um WalkerLead público.

    Usa get_global_db (rls_tenant="*") para poder resolver a indicação e o tenant
    sem restrição de tenant na sessão — idêntico a global_scope_session mas compatível
    com dependency_overrides (testável via TestClient).

    Após resolver o tenant, injeta set_session_tenant no GUC antes do INSERT.
    """
    # Rate limit por IP — mesmo mecanismo de candidaturas (10/10min por padrão).
    client_ip = _get_client_ip(request)
    if application_rate_limiter.is_blocked(client_ip):
        raise HTTPException(
            status_code=429,
            detail="Muitas tentativas. Tente novamente em alguns minutos.",
        )

    # Validação de input rigorosa — rota pública.
    name = (payload.name or "").strip()
    phone = (payload.phone or "").strip()
    city = (payload.city or "").strip() or None
    indication_id = (payload.indication_id or "").strip() or None
    tenant_slug = (payload.tenant_slug or "").strip() or None

    if not name:
        raise HTTPException(status_code=422, detail="nome é obrigatório.")
    if not phone:
        raise HTTPException(status_code=422, detail="telefone é obrigatório.")

    # Resolução de tenant: indication_id > tenant_slug > default.
    resolved_tenant_id: str | None = None
    indication: WalkerIndication | None = None

    if indication_id:
        indication = db.get(WalkerIndication, indication_id)
        if indication:
            resolved_tenant_id = indication.tenant_id

    if not resolved_tenant_id and tenant_slug:
        t = db.query(Tenant).filter(Tenant.slug == tenant_slug).first()
        if t:
            resolved_tenant_id = t.id

    if not resolved_tenant_id:
        # Fallback: primeiro tenant ativo encontrado (não chama ensure_default_tenant
        # para evitar DDL inesperado em testes com DB simples).
        first_tenant = db.query(Tenant).filter(Tenant.status == "active").first()
        if first_tenant:
            resolved_tenant_id = first_tenant.id

    if not resolved_tenant_id:
        raise HTTPException(status_code=400, detail="Tenant não pôde ser determinado.")

    # Injeta o escopo do tenant resolvido no GUC antes do INSERT (RLS).
    set_session_tenant(db, resolved_tenant_id)

    # Registra como tentativa (rate limit) APÓS resolver o tenant.
    application_rate_limiter.record_failure(client_ip)

    now = datetime.utcnow()
    lead = WalkerLead(
        id=str(uuid4()),
        tenant_id=resolved_tenant_id,
        name=name,
        phone=phone,
        city=city,
        indication_id=indication_id if indication else None,
        status="novo",
        created_at=now,
    )
    db.add(lead)
    db.flush()

    # Se há indicação válida, promove para lead_criado.
    if indication and indication.status == "enviada":
        indication.status = "lead_criado"
        indication.updated_at = now
        db.add(indication)

    _notify_admins_new_lead(db, lead, resolved_tenant_id)

    db.commit()

    # Resposta mínima: não vaza ids internos (rota pública).
    return {"ok": True}


@public_router.post("", status_code=201)
@api_public_router.post("", status_code=201)
def create_lead(
    payload: WalkerLeadCreate,
    request: Request,
    db: Session = Depends(get_global_db),
) -> dict:
    return _create_lead(payload, request, db)
