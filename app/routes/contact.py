"""Contato do site institucional — intake público de leads.

Espelha o padrão de partner-applications (POST público + rate limit por IP +
persistência) sem upload. O lead é gravado em ContactMessage; um admin lista via
GET protegido. Notificação por e-mail/n8n é um hook plugável (Sprint 18).
"""
import logging
import os
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.dependencies.rbac import require_permission
from app.dependencies.tenant_scope import apply_tenant_filter, get_admin_tenant_scope
from app.models.contact_message import ContactMessage
from app.models.user import User
from app.services.contact_notification_service import notify_new_contact
from app.services.login_rate_limiter import InMemoryLoginRateLimiter
from app.utils.registration_validation import normalize_email_or_raise

LOGGER = logging.getLogger(__name__)

router = APIRouter(prefix="/api/contact", tags=["contact"])

# Bucket de rate limit dedicado (separado do de uploads), por IP, in-memory.
contact_rate_limiter = InMemoryLoginRateLimiter(
    max_failures=int(os.getenv("CONTACT_RATE_LIMIT", "10")),
    window_seconds=float(os.getenv("CONTACT_RATE_WINDOW_SECONDS", "600")),
)


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _enforce_contact_rate_limit(request: Request) -> None:
    ip = _client_ip(request)
    if contact_rate_limiter.is_blocked(ip):
        raise HTTPException(status_code=429, detail="Muitas mensagens em pouco tempo. Tente novamente mais tarde.")
    contact_rate_limiter.record_failure(ip)


class ContactCreate(BaseModel):
    name: str = Field("", max_length=200)
    company: str = Field("", max_length=200)
    email: str = Field(..., max_length=200)
    phone: str = Field("", max_length=50)
    city: str = Field("", max_length=120)
    business_type: str = Field("", max_length=120)
    interest: str = Field("", max_length=200)
    message: str = Field("", max_length=5000)


class ContactCreateResponse(BaseModel):
    """api-T3: contrato estavel do intake publico de contato."""

    ok: bool
    id: str


@router.post("", status_code=201, response_model=ContactCreateResponse)
def create_contact(payload: ContactCreate, request: Request, db: Session = Depends(get_db)):
    _enforce_contact_rate_limit(request)
    try:
        email = normalize_email_or_raise(payload.email)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not (payload.name.strip() or payload.company.strip()):
        raise HTTPException(status_code=400, detail="Informe seu nome ou a empresa.")

    contact = ContactMessage(
        id=str(uuid4()),
        tenant_id=getattr(request.state, "tenant_id", None),
        name=payload.name.strip(),
        company=payload.company.strip(),
        email=email,
        phone=payload.phone.strip(),
        city=payload.city.strip(),
        business_type=payload.business_type.strip(),
        interest=payload.interest.strip(),
        message=payload.message.strip(),
        source="site",
        status="new",
    )
    db.add(contact)
    db.commit()
    db.refresh(contact)

    try:
        notify_new_contact(contact)
    except Exception:  # noqa: BLE001 - a notificação nunca pode quebrar o intake
        LOGGER.exception("falha ao notificar novo contato contact_id=%s", contact.id)

    return {"ok": True, "id": contact.id}


@router.get("")
def list_contacts(
    db: Session = Depends(get_db),
    admin: User = Depends(require_permission("tenants.read")),
):
    scope = get_admin_tenant_scope(admin)
    query = db.query(ContactMessage).order_by(ContactMessage.created_at.desc())
    rows = apply_tenant_filter(query, ContactMessage, scope).all()
    return [
        {
            "id": c.id,
            "name": c.name,
            "company": c.company,
            "email": c.email,
            "phone": c.phone,
            "city": c.city,
            "business_type": c.business_type,
            "interest": c.interest,
            "message": c.message,
            "status": c.status,
            "created_at": c.created_at,
        }
        for c in rows
    ]
