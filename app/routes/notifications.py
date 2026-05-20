import json
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.dependencies.auth import get_current_user
from app.models.notification import Notification
from app.models.push_token import PushToken
from app.models.user import User
from app.services.push_notifications import send_push_for_notification


router = APIRouter(prefix="/notifications", tags=["notifications"])
api_router = APIRouter(prefix="/api/notifications", tags=["notifications"])

ADMIN_NOTIFICATION_ROLES = {"admin", "super_admin", "superadmin"}


class NotificationCreate(BaseModel):
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
    notification = Notification(
        id=str(uuid.uuid4()),
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
    send_push_for_notification(db, notification)
    return notification


def _is_admin_role(role: str | None) -> bool:
    return role in ADMIN_NOTIFICATION_ROLES


def _visible_notifications_query(query, current_user: User):
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
    query = db.query(Notification)
    query = _visible_notifications_query(query, current_user)

    if only_unread:
        query = query.filter(Notification.is_read.is_(False))

    notifications = (
        query.order_by(Notification.created_at.desc())
        .limit(max(1, min(limit, 100)))
        .all()
    )

    return [_serialize_notification(notification) for notification in notifications]


def _unread_count(db: Session, current_user: User) -> dict[str, int]:
    query = db.query(Notification)
    query = _visible_notifications_query(query, current_user)

    count = query.filter(Notification.is_read.is_(False)).count()
    return {"count": count}


def _mark_as_read(db: Session, notification_id: str, current_user: User) -> dict[str, Any]:
    notification = db.query(Notification).filter(Notification.id == notification_id).first()

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
    notifications = db.query(Notification)
    notifications = _visible_notifications_query(notifications, current_user)

    rows = notifications.filter(Notification.is_read.is_(False)).all()

    now = datetime.utcnow()
    for notification in rows:
        notification.is_read = True
        notification.read_at = now
        db.add(notification)

    db.commit()

    return {"updated": len(rows)}


def _seed_demo_notifications(db: Session, current_user: User) -> list[dict[str, Any]]:
    existing = (
        db.query(Notification)
        .filter(Notification.user_id == current_user.id)
        .count()
    )

    if existing > 0:
        return _list_notifications(db, current_user)

    role = current_user.role or "tutor"

    if _is_admin_role(role):
        samples = [
            NotificationCreate(
                user_id=current_user.id,
                user_role="admin",
                title="Passeio entrou em recovery",
                message="Um passeio está aguardando ação operacional para continuar a busca por passeador.",
                type="recovery",
                related_entity_type="walk",
                related_entity_id="demo-walk-recovery",
                metadata={"priority": "high", "channel": "in_app"},
            ),
            NotificationCreate(
                user_id=current_user.id,
                user_role="admin",
                title="Muitas recusas detectadas",
                message="O sistema identificou várias recusas/expirações para o mesmo passeio.",
                type="operational_alert",
                related_entity_type="walk",
                related_entity_id="demo-walk-attempts",
                metadata={"priority": "medium", "channel": "in_app"},
            ),
        ]
    elif role == "walker":
        samples = [
            NotificationCreate(
                user_id=current_user.id,
                user_role="walker",
                title="Novo passeio disponível",
                message="Há um passeio disponível próximo à sua região. Responda antes do prazo expirar.",
                type="new_walk",
                related_entity_type="walk",
                related_entity_id="demo-walk-new",
                metadata={"priority": "medium", "channel": "in_app"},
            ),
            NotificationCreate(
                user_id=current_user.id,
                user_role="walker",
                title="Tempo de aceite acabando",
                message="Você tem pouco tempo para aceitar ou recusar este passeio.",
                type="acceptance_deadline",
                related_entity_type="walk",
                related_entity_id="demo-walk-deadline",
                metadata={"priority": "high", "channel": "in_app"},
            ),
        ]
    else:
        samples = [
            NotificationCreate(
                user_id=current_user.id,
                user_role="tutor",
                title="Passeio criado",
                message="Seu passeio foi criado e já estamos buscando um passeador disponível.",
                type="walk_created",
                related_entity_type="walk",
                related_entity_id="demo-walk-created",
                metadata={"priority": "low", "channel": "in_app"},
            ),
            NotificationCreate(
                user_id=current_user.id,
                user_role="tutor",
                title="Passeador aceitou",
                message="Um passeador aceitou o passeio e em breve estará a caminho.",
                type="walker_accepted",
                related_entity_type="walk",
                related_entity_id="demo-walk-accepted",
                metadata={"priority": "medium", "channel": "in_app"},
            ),
        ]

    for sample in samples:
        _create_notification(db, sample)

    return _list_notifications(db, current_user)


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


@router.post("")
@api_router.post("")
def create_notification(
    payload: NotificationCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not _is_admin_role(current_user.role):
        raise HTTPException(status_code=403, detail="Apenas admin pode criar notificações manualmente.")

    notification = _create_notification(db, payload)
    db.commit()
    db.refresh(notification)
    return _serialize_notification(notification)


@router.post("/seed-demo")
@api_router.post("/seed-demo")
def seed_demo_notifications(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    notifications = _seed_demo_notifications(db, current_user)
    db.commit()
    return notifications


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
