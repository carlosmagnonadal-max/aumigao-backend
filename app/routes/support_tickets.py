"""Tickets de suporte interno (Feature 2).

Modelo: SupportTicket (tabela support_tickets, criada em migration 0019).
Permissão base: require_permission("admin.access").
Tenant-scoping: get_admin_tenant_scope + apply_tenant_filter.

Rotas (dual-router /admin e /api/admin):
  GET    /admin/support-tickets          → lista com filtros opcionais + contadores
  POST   /admin/support-tickets          → cria ticket
  GET    /admin/support-tickets/{id}     → detalhe
  PATCH  /admin/support-tickets/{id}     → atualiza status, priority, assignee, notes
"""
from datetime import datetime
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.dependencies.rbac import require_permission
from app.dependencies.tenant_scope import (
    apply_tenant_filter,
    ensure_tenant_access,
    get_admin_tenant_scope,
)
from app.models.support_ticket import SupportTicket
from app.models.user import User

VALID_STATUSES = {"open", "in_progress", "resolved", "closed"}
VALID_PRIORITIES = {"low", "normal", "high"}
VALID_REQUESTER_ROLES = {"tutor", "walker", "interno"}

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
# Serialização
# ---------------------------------------------------------------------------

def _serialize_ticket(ticket: SupportTicket) -> dict:
    return {
        "id": ticket.id,
        "tenant_id": ticket.tenant_id,
        "subject": ticket.subject,
        "description": ticket.description,
        "requester_name": ticket.requester_name,
        "requester_email": ticket.requester_email,
        "requester_role": ticket.requester_role,
        "status": ticket.status,
        "priority": ticket.priority,
        "assignee_user_id": ticket.assignee_user_id,
        "internal_notes": ticket.internal_notes,
        "created_at": ticket.created_at.isoformat() if ticket.created_at else None,
        "updated_at": ticket.updated_at.isoformat() if ticket.updated_at else None,
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


class TicketUpdate(BaseModel):
    status: str | None = Field(None, pattern="^(open|in_progress|resolved|closed)$")
    priority: str | None = Field(None, pattern="^(low|normal|high)$")
    assignee_user_id: str | None = None
    internal_notes: str | None = None


# ---------------------------------------------------------------------------
# Rotas — padrão dual-router
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
    """
    scope = get_admin_tenant_scope(admin)
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

    # Para contadores, precisa de todos (sem o limit) — consulta separada só com scope/filtros de tenant
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
    scope = get_admin_tenant_scope(admin)
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
    scope = get_admin_tenant_scope(admin)
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
    """Atualiza status, priority, assignee_user_id e/ou internal_notes."""
    scope = get_admin_tenant_scope(admin)
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

    ticket.updated_at = datetime.utcnow()

    db.commit()
    db.refresh(ticket)
    return _serialize_ticket(ticket)
