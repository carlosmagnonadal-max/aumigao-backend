"""Tickets de suporte (Feature 2 + Fase 4 C1).

Modelo: SupportTicket (tabela support_tickets, criada em migration 0019; colunas
        user_id / reply / replied_at adicionadas em 0024_support_reply).

Rotas admin (dual-router /admin e /api/admin) — require_permission("admin.access"):
  GET    /admin/support-tickets          → lista com filtros opcionais + contadores
  POST   /admin/support-tickets          → cria ticket
  GET    /admin/support-tickets/{id}     → detalhe
  PATCH  /admin/support-tickets/{id}     → atualiza status, priority, assignee,
                                           internal_notes e/ou reply (reply→push)

Rotas user-facing (dual-router /support-tickets e /api/support-tickets) — get_current_user:
  POST   ""              → cria ticket do usuário logado (gate: feature support_tickets)
  GET    "/me"           → lista os tickets do usuário (id, subject, status, reply…)
"""
import logging
from datetime import datetime
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.dependencies.auth import get_current_user
from app.dependencies.rbac import require_permission
from app.dependencies.tenant_scope import (
    apply_tenant_filter,
    ensure_tenant_access,
    get_admin_tenant_scope,
)
from app.models.support_ticket import SupportTicket
from app.models.tenant import Tenant
from app.models.user import User
from app.services.login_rate_limiter import InMemoryLoginRateLimiter
from app.services.tenant_plan_service import tenant_feature_enabled

logger = logging.getLogger("app.routes.support_tickets")

VALID_STATUSES = {"open", "in_progress", "resolved", "closed"}
VALID_PRIORITIES = {"low", "normal", "high"}
VALID_REQUESTER_ROLES = {"tutor", "walker", "interno"}

# Rate limiter: 5 tickets por 15 minutos por user_id
_user_ticket_limiter = InMemoryLoginRateLimiter(max_failures=5, window_seconds=900)

# ---------------------------------------------------------------------------
# Routers — admin (existentes)
# ---------------------------------------------------------------------------
router = APIRouter(
    prefix="/admin",
    tags=["support-tickets"],
    dependencies=[Depends(require_permission("admin.access"))],
)
api_router = APIRouter(
    prefix="/api/admin",
    tags=["support-tickets"],
    dependencies=[Depends(require_permission("admin.access"))],
)

# ---------------------------------------------------------------------------
# Routers — user-facing (novos)
# ---------------------------------------------------------------------------
user_router = APIRouter(prefix="/support-tickets", tags=["support-tickets-user"])
api_user_router = APIRouter(prefix="/api/support-tickets", tags=["support-tickets-user"])


# ---------------------------------------------------------------------------
# Serialização
# ---------------------------------------------------------------------------

def _serialize_ticket(ticket: SupportTicket) -> dict:
    return {
        "id": ticket.id,
        "tenant_id": ticket.tenant_id,
        "user_id": ticket.user_id,
        "subject": ticket.subject,
        "description": ticket.description,
        "requester_name": ticket.requester_name,
        "requester_email": ticket.requester_email,
        "requester_role": ticket.requester_role,
        "status": ticket.status,
        "priority": ticket.priority,
        "assignee_user_id": ticket.assignee_user_id,
        "internal_notes": ticket.internal_notes,
        "reply": ticket.reply,
        "replied_at": ticket.replied_at.isoformat() if ticket.replied_at else None,
        "created_at": ticket.created_at.isoformat() if ticket.created_at else None,
        "updated_at": ticket.updated_at.isoformat() if ticket.updated_at else None,
    }


def _serialize_ticket_user(ticket: SupportTicket) -> dict:
    """Serialização reduzida para o app do usuário (sem internal_notes)."""
    return {
        "id": ticket.id,
        "subject": ticket.subject,
        "message": ticket.description,
        "status": ticket.status,
        "reply": ticket.reply,
        "replied_at": ticket.replied_at.isoformat() if ticket.replied_at else None,
        "created_at": ticket.created_at.isoformat() if ticket.created_at else None,
    }


def _build_status_counts(tickets: list[SupportTicket]) -> dict:
    counts: dict[str, int] = {s: 0 for s in ("open", "in_progress", "resolved", "closed")}
    for t in tickets:
        key = t.status or "open"
        if key in counts:
            counts[key] += 1
    return counts


# ---------------------------------------------------------------------------
# Schemas Pydantic
# ---------------------------------------------------------------------------

class TicketCreate(BaseModel):
    subject: str = Field(..., min_length=1, max_length=500)
    description: str = Field(..., min_length=1)
    requester_name: str | None = Field(None, max_length=200)
    requester_email: str | None = Field(None, max_length=254)
    requester_role: str | None = Field(None, pattern="^(tutor|walker|interno)$")
    priority: str = Field("normal", pattern="^(low|normal|high)$")


class UserTicketCreate(BaseModel):
    subject: str = Field(..., min_length=1, max_length=500)
    message: str = Field(..., min_length=1)
    category: str | None = Field(None, max_length=120)


class TicketUpdate(BaseModel):
    status: str | None = Field(None, pattern="^(open|in_progress|resolved|closed)$")
    priority: str | None = Field(None, pattern="^(low|normal|high)$")
    assignee_user_id: str | None = None
    internal_notes: str | None = None
    reply: str | None = None


# ---------------------------------------------------------------------------
# Helper: notificação de resposta ao usuário
# ---------------------------------------------------------------------------

def _notify_support_reply(db: Session, ticket: SupportTicket) -> None:
    """Cria notificação push para o autor do ticket quando admin envia reply.

    Idempotente: não cria segunda notificação se replied_at não mudou.
    Importa _create_notification localmente para evitar ciclo de imports.
    """
    if not ticket.user_id or not ticket.reply:
        return

    from app.routes.notifications import NotificationCreate, _create_notification

    notif_payload = NotificationCreate(
        user_id=ticket.user_id,
        user_role="tutor",  # tutor ou walker — push vai pelo user_id de qualquer forma
        title="A equipe respondeu seu chamado",
        message=ticket.reply[:200],  # trunca para o push
        type="support_reply",
        related_entity_type="support_ticket",
        related_entity_id=ticket.id,
        metadata={"subject": ticket.subject},
    )
    _create_notification(db, notif_payload)
    logger.info(
        "notificação support_reply criada para user_id=%s ticket_id=%s",
        ticket.user_id,
        ticket.id,
    )


# ---------------------------------------------------------------------------
# Rotas ADMIN — padrão dual-router
# ---------------------------------------------------------------------------

@router.get("/support-tickets")
@api_router.get("/support-tickets")
def list_support_tickets(
    status: str | None = Query(None),
    priority: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    admin: User = Depends(require_permission("admin.access")),
    db: Session = Depends(get_db),
):
    """Lista tickets com filtros opcionais por status/priority.

    Retorna items + status_counts (resumo de contadores por status).
    Tenant-scoped: admin regular vê só seu tenant; super_admin vê todos.
    """
    scope = get_admin_tenant_scope(admin, db)
    query = db.query(SupportTicket)
    query = apply_tenant_filter(query, SupportTicket, scope)

    if status:
        if status not in VALID_STATUSES:
            raise HTTPException(status_code=400, detail=f"Status invalido. Use: {sorted(VALID_STATUSES)}")
        query = query.filter(SupportTicket.status == status)

    if priority:
        if priority not in VALID_PRIORITIES:
            raise HTTPException(status_code=400, detail=f"Priority invalida. Use: {sorted(VALID_PRIORITIES)}")
        query = query.filter(SupportTicket.priority == priority)

    # Contadores separados (sem filtro de status/priority)
    count_query = db.query(SupportTicket)
    count_query = apply_tenant_filter(count_query, SupportTicket, scope)
    all_tickets_for_counts = count_query.all()

    tickets = query.order_by(SupportTicket.created_at.desc()).limit(limit).all()

    return {
        "items": [_serialize_ticket(t) for t in tickets],
        "total": len(tickets),
        "status_counts": _build_status_counts(all_tickets_for_counts),
    }


@router.post("/support-tickets", status_code=201)
@api_router.post("/support-tickets", status_code=201)
def create_support_ticket(
    payload: TicketCreate,
    admin: User = Depends(require_permission("admin.access")),
    db: Session = Depends(get_db),
):
    """Cria novo ticket de suporte. tenant_id = escopo do admin autenticado."""
    scope = get_admin_tenant_scope(admin, db)
    tenant_id = scope.tenant_id  # None para super_admin (ticket global)

    ticket = SupportTicket(
        id=str(uuid4()),
        tenant_id=tenant_id,
        subject=payload.subject.strip(),
        description=payload.description.strip(),
        requester_name=(payload.requester_name or "").strip() or None,
        requester_email=(payload.requester_email or "").strip() or None,
        requester_role=payload.requester_role,
        status="open",
        priority=payload.priority,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(ticket)
    db.commit()
    db.refresh(ticket)
    return _serialize_ticket(ticket)


@router.get("/support-tickets/{ticket_id}")
@api_router.get("/support-tickets/{ticket_id}")
def get_support_ticket(
    ticket_id: str,
    admin: User = Depends(require_permission("admin.access")),
    db: Session = Depends(get_db),
):
    """Detalhe de um ticket. Verifica acesso por tenant."""
    scope = get_admin_tenant_scope(admin, db)
    ticket = db.get(SupportTicket, ticket_id)
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket nao encontrado.")
    ensure_tenant_access(ticket.tenant_id, scope)
    return _serialize_ticket(ticket)


@router.patch("/support-tickets/{ticket_id}")
@api_router.patch("/support-tickets/{ticket_id}")
def update_support_ticket(
    ticket_id: str,
    payload: TicketUpdate,
    admin: User = Depends(require_permission("admin.access")),
    db: Session = Depends(get_db),
):
    """Atualiza status, priority, assignee_user_id, internal_notes e/ou reply.

    Ao salvar reply não-vazia pela primeira vez (ou atualizada):
    - seta replied_at = now
    - cria Notification + push para o autor (type=support_reply)
    """
    scope = get_admin_tenant_scope(admin, db)
    ticket = db.get(SupportTicket, ticket_id)
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket nao encontrado.")
    ensure_tenant_access(ticket.tenant_id, scope)

    if payload.status is not None:
        ticket.status = payload.status

    if payload.priority is not None:
        ticket.priority = payload.priority

    if payload.assignee_user_id is not None:
        ticket.assignee_user_id = payload.assignee_user_id or None

    if payload.internal_notes is not None:
        ticket.internal_notes = payload.internal_notes

    # Resposta pública ao usuário
    send_reply_notification = False
    if payload.reply is not None:
        new_reply = payload.reply.strip() or None
        if new_reply and new_reply != ticket.reply:
            ticket.reply = new_reply
            ticket.replied_at = datetime.utcnow()
            send_reply_notification = True
        elif not new_reply:
            ticket.reply = None

    ticket.updated_at = datetime.utcnow()

    db.commit()
    db.refresh(ticket)

    if send_reply_notification:
        try:
            _notify_support_reply(db, ticket)
        except Exception:
            logger.exception(
                "falha ao criar notificação support_reply ticket_id=%s", ticket.id
            )

    return _serialize_ticket(ticket)


# ---------------------------------------------------------------------------
# Rotas USER-FACING — dual-router /support-tickets e /api/support-tickets
# ---------------------------------------------------------------------------

@user_router.post("", status_code=201)
@api_user_router.post("", status_code=201)
def user_create_support_ticket(
    payload: UserTicketCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Cria ticket de suporte para o usuário logado.

    Gate: feature support_tickets deve estar habilitada para o tenant.
    Rate limit: 5 tickets por 15 minutos por user_id.
    """
    # Gate de feature
    tenant: Tenant | None = db.get(Tenant, current_user.tenant_id) if current_user.tenant_id else None
    if tenant and not tenant_feature_enabled(tenant, db, "support_tickets"):
        raise HTTPException(status_code=403, detail="Suporte via tickets nao esta habilitado para esta operacao.")

    # Rate limit
    if _user_ticket_limiter.is_blocked(current_user.id):
        raise HTTPException(
            status_code=429,
            detail="Muitas solicitacoes em pouco tempo. Tente novamente em 15 minutos.",
        )
    _user_ticket_limiter.record_failure(current_user.id)

    # Determina role do solicitante. Fallback "interno" cobre admin/super_admin
    # do tenant (que não é tutor nem walker mas pode abrir ticket pelo app).
    # Mapeamento espelha o que o admin-web usa para classificar quem abriu.
    requester_role: str | None = None
    if current_user.role in {"tutor"}:
        requester_role = "tutor"
    elif current_user.role in {"walker", "passeador"}:
        requester_role = "walker"
    elif current_user.role in {"admin", "super_admin", "interno"}:
        requester_role = "interno"

    subject = (payload.subject or "").strip()
    if payload.category:
        subject = f"[{payload.category.strip()}] {subject}"

    ticket = SupportTicket(
        id=str(uuid4()),
        tenant_id=current_user.tenant_id,
        user_id=current_user.id,
        subject=subject[:500],
        description=payload.message.strip(),
        requester_name=current_user.full_name or None,
        requester_email=current_user.email or None,
        requester_role=requester_role,
        status="open",
        priority="normal",
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(ticket)
    db.commit()
    db.refresh(ticket)
    logger.info(
        "ticket criado pelo usuário user_id=%s tenant_id=%s ticket_id=%s",
        current_user.id,
        current_user.tenant_id,
        ticket.id,
    )
    return _serialize_ticket_user(ticket)


@user_router.get("/me")
@api_user_router.get("/me")
def user_list_my_tickets(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Lista os tickets do usuário no tenant ATUAL (máx 50, desc).

    Filtra por user_id E por tenant_id atual. Decisão: tickets pertencem ao
    BINÔMIO (user, tenant) — quando o usuário troca de tenant, os tickets
    antigos não migram (histórico fica com o tenant de origem, visível
    apenas para admin daquele tenant). Sem isso, um tutor que troca de
    PMG→Aumigão continuaria vendo tickets antigos do PMG (UX confusa:
    "por que minha solicitação antiga não tem nada a ver com este tenant?").

    Retorna apenas os campos relevantes para o app (sem internal_notes).
    """
    tickets = (
        db.query(SupportTicket)
        .filter(SupportTicket.user_id == current_user.id)
        .filter(SupportTicket.tenant_id == current_user.tenant_id)
        .order_by(SupportTicket.created_at.desc())
        .limit(50)
        .all()
    )
    return [_serialize_ticket_user(t) for t in tickets]
