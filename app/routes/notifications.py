import json
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.dependencies.auth import get_current_user
from app.models.notification import Notification
from app.models.push_token import PushToken
from app.models.user import User
from app.services.push_notifications import send_push_for_notification, send_push_for_notification_background
from app.services.tenant_context import resolve_current_tenant_id


router = APIRouter(prefix="/notifications", tags=["notifications"])
api_router = APIRouter(prefix="/api/notifications", tags=["notifications"])

ADMIN_NOTIFICATION_ROLES = {"admin", "super_admin", "superadmin"}


class NotificationCreate(BaseModel):
    tenant_id: str | None = None
    user_id: str | None = None
    user_role: str = Field(default="tutor")
    title: str
    message: str
    type: str = Field(default="info")
    related_entity_type: str | None = None
    related_entity_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class PushTokenCreate(BaseModel):
    expo_push_token: str
    platform: str = "unknown"


class NotificationResponse(BaseModel):
    id: str
    user_id: str | None
    user_role: str
    title: str
    message: str
    type: str
    related_entity_type: str | None
    related_entity_id: str | None
    metadata: dict[str, Any]
    is_read: bool
    read_at: datetime | None
    created_at: datetime


def _parse_metadata(metadata_json: str | None) -> dict[str, Any]:
    if not metadata_json:
        return {}
    try:
        parsed = json.loads(metadata_json)
        if isinstance(parsed, dict):
            return parsed
        return {}
    except Exception:
        return {}


def _serialize_notification(notification: Notification) -> dict[str, Any]:
    return {
        "id": notification.id,
        "user_id": notification.user_id,
        "user_role": notification.user_role,
        "title": notification.title,
        "message": notification.message,
        "type": notification.type,
        "related_entity_type": notification.related_entity_type,
        "related_entity_id": notification.related_entity_id,
        "metadata": _parse_metadata(notification.metadata_json),
        "is_read": notification.is_read,
        "read_at": notification.read_at,
        "created_at": notification.created_at,
    }


def _create_notification(db: Session, payload: NotificationCreate) -> Notification:
    user = db.get(User, payload.user_id) if payload.user_id else None
    tenant_id = payload.tenant_id or (user.tenant_id if user else None) or resolve_current_tenant_id(db)
    notification = Notification(
        id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        user_id=payload.user_id,
        user_role=payload.user_role,
        title=payload.title,
        message=payload.message,
        type=payload.type,
        related_entity_type=payload.related_entity_type,
        related_entity_id=payload.related_entity_id,
        metadata_json=json.dumps(payload.metadata or {}, ensure_ascii=False, default=str),
        created_at=datetime.utcnow(),
    )
    db.add(notification)
    db.flush()
    send_push_for_notification_background(db, notification)
    return notification


def _is_admin_role(role: str | None) -> bool:
    return role in ADMIN_NOTIFICATION_ROLES


def _current_tenant_id(current_user: User, db: Session) -> str:
    """Retorna o tenant ATIVO da request (request.state.tenant_id, injetado em
    db.info["rls_tenant"] pelo get_db dependency).

    NÃO usa current_user.tenant_id: o usuário tem um tenant de NASCIMENTO, mas no
    modelo multi-tenant (Modelo B) pode operar em outro tenant via X-Tenant-Slug.
    Usar o tenant_id do usuário (fixo) causaria cross-tenant leak: notificações do
    tenant A apareceriam enquanto o usuário está operando no tenant B.

    Hierarquia: rls_tenant (da request) → tenant_id do usuário (fallback de
    compatibilidade para chamadores internos sem request, e.g., schedulers) →
    default tenant.
    """
    rls_tenant = db.info.get("rls_tenant")
    # rls_tenant pode ser "*" (super_admin global) ou "" (fail-closed sem tenant) —
    # nesses casos caímos no fallback do user.
    if rls_tenant and rls_tenant not in ("*", ""):
        return rls_tenant
    return current_user.tenant_id or resolve_current_tenant_id(db)


def _notification_tenant_filter(tenant_id: str):
    return or_(Notification.tenant_id == tenant_id, Notification.tenant_id.is_(None))


def _visible_notifications_query(query, current_user: User, tenant_id: str):
    query = query.filter(_notification_tenant_filter(tenant_id))
    if _is_admin_role(current_user.role):
        return query.filter(
            (Notification.user_id == current_user.id)
            | (
                Notification.user_id.is_(None)
                & Notification.user_role.in_(list(ADMIN_NOTIFICATION_ROLES))
            )
        )
    return query.filter(Notification.user_id == current_user.id)


def _list_notifications(
    db: Session,
    current_user: User,
    only_unread: bool = False,
    limit: int = 50,
) -> list[dict[str, Any]]:
    tenant_id = _current_tenant_id(current_user, db)
    query = db.query(Notification)
    query = _visible_notifications_query(query, current_user, tenant_id)

    if only_unread:
        query = query.filter(Notification.is_read.is_(False))

    notifications = (
        query.order_by(Notification.created_at.desc())
        .limit(max(1, min(limit, 100)))
        .all()
    )

    return [_serialize_notification(notification) for notification in notifications]


def _unread_count(db: Session, current_user: User) -> dict[str, int]:
    tenant_id = _current_tenant_id(current_user, db)
    query = db.query(Notification)
    query = _visible_notifications_query(query, current_user, tenant_id)

    count = query.filter(Notification.is_read.is_(False)).count()
    return {"count": count}


def _mark_as_read(db: Session, notification_id: str, current_user: User) -> dict[str, Any]:
    tenant_id = _current_tenant_id(current_user, db)
    notification = db.query(Notification).filter(Notification.id == notification_id, _notification_tenant_filter(tenant_id)).first()

    if not notification:
        raise HTTPException(status_code=404, detail="Notificação não encontrada.")

    is_admin = _is_admin_role(current_user.role)
    owns_notification = notification.user_id == current_user.id
    is_global_admin_notification = notification.user_id is None and notification.user_role in ADMIN_NOTIFICATION_ROLES

    if not owns_notification and not (is_admin and is_global_admin_notification):
        raise HTTPException(status_code=403, detail="Você não pode alterar esta notificação.")

    notification.is_read = True
    notification.read_at = datetime.utcnow()

    db.add(notification)
    db.commit()
    db.refresh(notification)

    return _serialize_notification(notification)


def _mark_all_as_read(db: Session, current_user: User) -> dict[str, int]:
    tenant_id = _current_tenant_id(current_user, db)
    notifications = db.query(Notification)
    notifications = _visible_notifications_query(notifications, current_user, tenant_id)

    rows = notifications.filter(Notification.is_read.is_(False)).all()

    now = datetime.utcnow()
    for notification in rows:
        notification.is_read = True
        notification.read_at = now
        db.add(notification)

    db.commit()

    return {"updated": len(rows)}


@router.get("")
@api_router.get("")
def get_notifications(
    only_unread: bool = False,
    limit: int = 50,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return _list_notifications(db, current_user, only_unread=only_unread, limit=limit)


@router.get("/unread-count")
@api_router.get("/unread-count")
def get_unread_count(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return _unread_count(db, current_user)


@router.post("/push-token")
@api_router.post("/push-token")
def register_push_token(
    payload: PushTokenCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    token = payload.expo_push_token.strip()
    if not token:
        raise HTTPException(status_code=400, detail="Token de push invalido.")

    row = db.query(PushToken).filter(PushToken.expo_push_token == token).first()
    if not row:
        row = PushToken(
            id=str(uuid.uuid4()),
            user_id=current_user.id,
            expo_push_token=token,
        )
        db.add(row)

    row.user_id = current_user.id
    row.platform = (payload.platform or "unknown").strip() or "unknown"
    row.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(row)
    return {"ok": True, "user_id": row.user_id, "platform": row.platform, "updated_at": row.updated_at}


@router.post("/push-test")
@api_router.post("/push-test")
def create_push_test_notification(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    has_token = (
        db.query(PushToken)
        .filter(PushToken.user_id == current_user.id)
        .first()
        is not None
    )
    if not has_token:
        raise HTTPException(
            status_code=409,
            detail="Nenhum token push registrado para este usuário. Ative as notificações no dispositivo físico e tente novamente.",
        )

    notification = _create_notification(
        db,
        NotificationCreate(
            user_id=current_user.id,
            user_role=current_user.role or "tutor",
            title="Teste de notificação",
            message="Se você recebeu este alerta no dispositivo físico, o push real está ativo para este perfil.",
            type="push_test",
            metadata={"origin": "manual_push_test", "priority": "high"},
        ),
    )
    db.commit()
    db.refresh(notification)
    return {"ok": True, "notification": _serialize_notification(notification)}


@router.post("")
@api_router.post("")
def create_notification(
    payload: NotificationCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not _is_admin_role(current_user.role):
        raise HTTPException(status_code=403, detail="Apenas admin pode criar notificações manualmente.")

    # Isolamento: admin de tenant nao pode criar notificacoes para tenant arbitrario.
    # super_admin pode criar para qualquer tenant (payload.tenant_id livre).
    if current_user.role not in {"super_admin", "superadmin"}:
        admin_tenant = current_user.tenant_id or _current_tenant_id(current_user, db)
        if payload.tenant_id and payload.tenant_id != admin_tenant:
            raise HTTPException(
                status_code=403,
                detail="Admin de tenant nao pode criar notificacoes para outro tenant.",
            )
        # Sobrescreve silenciosamente para garantir isolamento.
        payload = payload.model_copy(update={"tenant_id": admin_tenant})

    notification = _create_notification(db, payload)
    db.commit()
    db.refresh(notification)
    return _serialize_notification(notification)


@router.patch("/{notification_id}/read")
@api_router.patch("/{notification_id}/read")
def mark_notification_as_read(
    notification_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return _mark_as_read(db, notification_id, current_user)


@router.patch("/read-all")
@api_router.patch("/read-all")
def mark_all_notifications_as_read(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return _mark_all_as_read(db, current_user)
