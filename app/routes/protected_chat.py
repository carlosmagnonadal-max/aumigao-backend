import json
from datetime import datetime, timedelta
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.dependencies.auth import get_current_user
from app.models.notification import Notification
from app.models.protected_chat_message import ProtectedChatMessage
from app.models.tenant import Tenant
from app.models.user import User
from app.models.walk import Walk
from app.services.push_notifications import send_push_for_notification_background
from app.services.tenant_plan_service import tenant_feature_enabled
from app.services.tenant_seed_service import default_tenant_id


router = APIRouter(prefix="/protected-chat", tags=["protected-chat"])
api_router = APIRouter(prefix="/api/protected-chat", tags=["protected-chat"])

CHAT_OPEN_BEFORE_MINUTES = 30
CHAT_AFTER_COMPLETION_MINUTES = 30
ALLOWED_CHAT_STATUSES = {
    "walker_accepted",
    "ride_scheduled",
    "walker_arriving",
    "walker_heading_to_pickup",
    "ride_in_progress",
    "ride_completed",
}
CANCELLED_STATUSES = {
    "cancelled",
    "canceled",
    "cancelado",
    "ride_cancelled",
    "ride_canceled",
}


class ProtectedChatMessageCreate(BaseModel):
    walk_id: str
    body: str = Field(min_length=1, max_length=1000)


class MarkMessagesReadPayload(BaseModel):
    walk_id: str


def _normalize_role(role: str | None) -> str:
    normalized = str(role or "").strip().lower()
    if normalized in {"cliente", "tutor"}:
        return "tutor"
    if normalized in {"passeador", "walker"}:
        return "walker"
    return normalized


def _scheduled_start_utc(db: Session, walk: Walk) -> datetime | None:
    """INÍCIO do passeio em UTC naive (scheduled_date é hora LOCAL do tenant)."""
    from app.lib.walk_time import tenant_tz_name, walk_start_utc

    return walk_start_utc(walk.scheduled_date, tenant_tz_name(db, walk.tenant_id))


def _walk_status(walk: Walk) -> str:
    return str(walk.operational_status or walk.status or "").strip().lower()


def _participant_role(walk: Walk, user: User) -> str:
    user_id = str(user.id)
    if walk.tutor_id == user_id:
        return "tutor"
    walker_ids = {value for value in [walk.walker_id, walk.assigned_walker_id] if value}
    if user_id in walker_ids:
        return "walker"
    raise HTTPException(status_code=403, detail="Apenas tutor e passeador vinculados podem acessar este chat.")


def _accepted_walker_id(walk: Walk) -> str | None:
    return walk.assigned_walker_id or walk.walker_id


def _assert_chat_available(walk: Walk, user: User, db: Session) -> str:
    participant_role = _participant_role(walk, user)
    status = _walk_status(walk)

    if status in CANCELLED_STATUSES or "cancel" in status:
        raise HTTPException(status_code=403, detail="Chat indisponivel para passeio cancelado.")

    if not _accepted_walker_id(walk):
        raise HTTPException(status_code=403, detail="Chat sera liberado apos confirmacao do passeador.")

    if status not in ALLOWED_CHAT_STATUSES:
        raise HTTPException(status_code=403, detail="Chat disponivel apenas durante a janela operacional do passeio.")

    scheduled_at = _scheduled_start_utc(db, walk)
    if not scheduled_at:
        raise HTTPException(status_code=403, detail="Chat indisponivel ate confirmacao do horario do passeio.")

    now = datetime.utcnow()
    opens_at = scheduled_at - timedelta(minutes=CHAT_OPEN_BEFORE_MINUTES)
    if now < opens_at:
        raise HTTPException(status_code=403, detail="Chat disponivel 30 minutos antes do passeio.")

    if status == "ride_completed":
        closes_at = scheduled_at + timedelta(minutes=walk.duration_minutes or 0) + timedelta(minutes=CHAT_AFTER_COMPLETION_MINUTES)
        if now > closes_at:
            raise HTTPException(status_code=403, detail="Chat encerrado para este passeio.")

    return participant_role


def _serialize_message(message: ProtectedChatMessage) -> dict:
    return {
        "id": message.id,
        "walk_id": message.walk_id,
        "sender_user_id": message.sender_user_id,
        "sender_role": message.sender_role,
        "body": message.body,
        "created_at": message.created_at,
        "read_at": message.read_at,
    }


def _other_participant_id(walk: Walk, sender_id: str) -> tuple[str | None, str]:
    if sender_id == walk.tutor_id:
        return _accepted_walker_id(walk), "walker"
    return walk.tutor_id, "tutor"


def _build_in_app_notification(db: Session, walk: Walk, sender: User, message: ProtectedChatMessage) -> Notification | None:
    """Constroi (mas nao persiste) uma Notification de chat para o outro participante."""
    recipient_id, recipient_role = _other_participant_id(walk, sender.id)
    if not recipient_id:
        return None
    sender_label = "Passeador" if message.sender_role == "walker" else "Tutor"
    return Notification(
        id=str(uuid4()),
        tenant_id=walk.tenant_id or sender.tenant_id or default_tenant_id(db),
        user_id=recipient_id,
        user_role=recipient_role,
        title="Nova mensagem no chat do passeio",
        message=f"{sender_label} enviou uma mensagem no chat do passeio.",
        type="protected_chat_message",
        related_entity_type="walk",
        related_entity_id=walk.id,
        metadata_json=json.dumps({"message_id": message.id, "sender_role": message.sender_role}),
        created_at=datetime.utcnow(),
    )


def _get_walk_or_404(db: Session, walk_id: str) -> Walk:
    walk = db.get(Walk, walk_id)
    if not walk:
        raise HTTPException(status_code=404, detail="Passeio nao encontrado.")
    return walk


def _assert_protected_chat_feature(walk: Walk, user: User, db: Session) -> None:
    tenant_id = walk.tenant_id or user.tenant_id
    if not tenant_id:
        return
    tenant = db.get(Tenant, tenant_id)
    if tenant and not tenant_feature_enabled(tenant, db, "protected_chat"):
        raise HTTPException(status_code=403, detail="Chat protegido não está habilitado para este tenant.")


def _mark_walk_messages_read(walk_id: str, user_id: str, db: Session) -> int:
    """Marca como lidas todas as mensagens do walk enviadas pelo outro participante.

    Retorna o numero de mensagens marcadas.
    """
    messages = (
        db.query(ProtectedChatMessage)
        .filter(
            ProtectedChatMessage.walk_id == walk_id,
            ProtectedChatMessage.sender_user_id != user_id,
            ProtectedChatMessage.read_at.is_(None),
        )
        .all()
    )
    if not messages:
        return 0
    now = datetime.utcnow()
    for message in messages:
        message.read_at = now
        db.add(message)
    db.commit()
    return len(messages)


def list_messages(walk_id: str, user: User, db: Session) -> dict:
    walk = _get_walk_or_404(db, walk_id)
    _assert_protected_chat_feature(walk, user, db)
    _assert_chat_available(walk, user, db)
    messages = (
        db.query(ProtectedChatMessage)
        .filter(ProtectedChatMessage.walk_id == walk.id)
        .order_by(ProtectedChatMessage.created_at.asc())
        .all()
    )
    # Conta nao-lidas ANTES de marcar (perspectiva do usuario autenticado)
    unread_count = sum(
        1 for m in messages
        if m.sender_user_id != user.id and m.read_at is None
    )
    now = datetime.utcnow()
    changed = False
    for message in messages:
        if message.sender_user_id != user.id and message.read_at is None:
            message.read_at = now
            db.add(message)
            changed = True
    if changed:
        db.commit()
    return {
        "items": [_serialize_message(message) for message in messages],
        "chat_available": True,
        "unread_count": unread_count,
    }


def count_unread_messages(walk_id: str, user: User, db: Session) -> dict:
    """Contador de não-lidas SEM efeito colateral (read-only).

    list_messages marca como lidas ao servir o GET; o app chamava aquele endpoint
    só para o badge, zerando o contador no ato. Este endpoint conta sem marcar nada.
    Mesmos gates de acesso do list (feature + janela do chat), para ser um
    substituto direto na contagem.
    """
    walk = _get_walk_or_404(db, walk_id)
    _assert_protected_chat_feature(walk, user, db)
    _assert_chat_available(walk, user, db)
    unread_count = (
        db.query(ProtectedChatMessage)
        .filter(
            ProtectedChatMessage.walk_id == walk.id,
            ProtectedChatMessage.sender_user_id != user.id,
            ProtectedChatMessage.read_at.is_(None),
        )
        .count()
    )
    return {"unread_count": unread_count}


def mark_messages_read(walk_id: str, user: User, db: Session) -> dict:
    walk = _get_walk_or_404(db, walk_id)
    _assert_protected_chat_feature(walk, user, db)
    _assert_chat_available(walk, user, db)
    marked = _mark_walk_messages_read(walk.id, user.id, db)
    return {"marked": marked}


def create_message(payload: ProtectedChatMessageCreate, user: User, db: Session) -> dict:
    walk = _get_walk_or_404(db, payload.walk_id)
    _assert_protected_chat_feature(walk, user, db)
    participant_role = _assert_chat_available(walk, user, db)
    body = payload.body.strip()
    if not body:
        raise HTTPException(status_code=400, detail="Mensagem vazia.")
    message = ProtectedChatMessage(
        id=str(uuid4()),
        walk_id=walk.id,
        sender_user_id=user.id,
        sender_role=participant_role or _normalize_role(user.role),
        body=body,
        created_at=datetime.utcnow(),
        # Propagado do walk para satisfazer a policy RLS (0046).
        tenant_id=walk.tenant_id,
    )
    db.add(message)
    db.flush()
    notification = _build_in_app_notification(db, walk, user, message)
    if notification:
        db.add(notification)
    db.commit()
    db.refresh(message)
    if notification:
        send_push_for_notification_background(db, notification)
    return _serialize_message(message)


@router.get("/messages")
def get_protected_chat_messages(
    walk_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return list_messages(walk_id, user, db)


@router.post("/messages")
def post_protected_chat_message(
    payload: ProtectedChatMessageCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return create_message(payload, user, db)


@router.post("/messages/read")
def post_mark_messages_read(
    payload: MarkMessagesReadPayload,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return mark_messages_read(payload.walk_id, user, db)


@router.get("/unread-count")
def get_unread_count(
    walk_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return count_unread_messages(walk_id, user, db)


@api_router.get("/messages")
def get_api_protected_chat_messages(
    walk_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return list_messages(walk_id, user, db)


@api_router.post("/messages")
def post_api_protected_chat_message(
    payload: ProtectedChatMessageCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return create_message(payload, user, db)


@api_router.post("/messages/read")
def post_api_mark_messages_read(
    payload: MarkMessagesReadPayload,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return mark_messages_read(payload.walk_id, user, db)


@api_router.get("/unread-count")
def get_api_unread_count(
    walk_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return count_unread_messages(walk_id, user, db)
