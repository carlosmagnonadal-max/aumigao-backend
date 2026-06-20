from __future__ import annotations

import json
import logging
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from sqlalchemy.orm import Session

from app.models.notification import Notification
from app.models.push_token import PushToken
from app.models.tenant import Tenant
from app.models.user import User
from app.services.operational_observability_service import record_operational_exception, record_operational_log

LOGGER = logging.getLogger("aumigao.push_notifications")
EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"
CRITICAL_NOTIFICATION_TYPES = {
    "push_test",
    "new_walk",
    "walker_attempt_created",
    "walker_accepted",
    "walk_completion_review_pending",
    "walk_completion_review_rejected",
    "walk_completion_review_approved",
    "walk_payment_released",
    "payment_confirmed",
    "support_reply",
    "protected_chat_message",
    # Fase 7 $-2: gorjeta confirmada — notificação crítica para o walker.
    "tip_received",
}
CRITICAL_WALK_STATUS_ACTIONS = {"walker_accepted", "ride_in_progress"}

# ThreadPoolExecutor module-level para envio fire-and-forget.
# daemon=True: threads nao impedem shutdown do processo.
_PUSH_EXECUTOR = ThreadPoolExecutor(max_workers=3, thread_name_prefix="push_worker")


def _metadata(notification: Notification) -> dict[str, Any]:
    try:
        parsed = json.loads(notification.metadata_json or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _should_push(notification: Notification) -> bool:
    if notification.type in CRITICAL_NOTIFICATION_TYPES:
        return True
    metadata = _metadata(notification)
    return notification.type == "walk_status" and metadata.get("action") in CRITICAL_WALK_STATUS_ACTIONS


ADMIN_NOTIFICATION_ROLES = {"admin", "super_admin", "superadmin"}


def _tokens_for_notification(db: Session, notification: Notification) -> list[PushToken]:
    query = db.query(PushToken)
    if notification.user_id:
        query = query.filter(PushToken.user_id == notification.user_id)
    elif notification.user_role in ADMIN_NOTIFICATION_ROLES:
        query = query.join(User, User.id == PushToken.user_id).filter(User.role.in_(ADMIN_NOTIFICATION_ROLES))
    else:
        return []
    return [row for row in query.all() if row.expo_push_token]


def _remove_invalid_push_token(db: Session, token: str, notification: Notification, reason: str) -> None:
    row = db.query(PushToken).filter(PushToken.expo_push_token == token).first()
    if not row:
        return
    db.delete(row)
    record_operational_log(
        db,
        event_type="push_token_invalidated",
        severity="warning",
        source="push_notifications",
        message="Token Expo removido após retorno de dispositivo inválido.",
        context={"notification_id": notification.id, "token_id": row.id, "user_id": row.user_id, "reason": reason},
    )


def _handle_expo_response(db: Session, notification: Notification, token_rows: list[PushToken], response_payload: dict[str, Any]) -> None:
    tickets = response_payload.get("data")
    if isinstance(tickets, dict):
        tickets = [tickets]
    if not isinstance(tickets, list):
        return

    for token_row, ticket in zip(token_rows, tickets):
        if not isinstance(ticket, dict) or ticket.get("status") != "error":
            continue
        details = ticket.get("details") if isinstance(ticket.get("details"), dict) else {}
        error_code = str(details.get("error") or ticket.get("message") or "expo_push_error")
        if error_code == "DeviceNotRegistered":
            _remove_invalid_push_token(db, token_row.expo_push_token, notification, error_code)
            continue
        record_operational_log(
            db,
            event_type="push_failed",
            severity="warning",
            source="push_notifications",
            message=str(ticket.get("message") or error_code),
            context={"notification_id": notification.id, "type": notification.type, "expo_error": error_code},
        )


def _do_send(messages: list[dict], notification_id: str, notification_type: str, token_strings: list[str], session_factory) -> None:
    """Executa o HTTP call e registra resultado numa sessao propria (thread-safe).

    token_strings: lista de strings expo_push_token (NÃO objetos ORM — a sessão
    original já foi fechada quando este thread executa).
    """
    from app.core.database import SessionLocal as _SessionLocal  # import local para evitar ciclo na inicializacao
    factory = session_factory or _SessionLocal
    db = factory()
    # Fase 2c: envio de push é operação de plataforma (cross-tenant) → acesso irrestrito.
    db.info["rls_tenant"] = "*"
    try:
        try:
            request = urllib.request.Request(
                EXPO_PUSH_URL,
                data=json.dumps(messages).encode("utf-8"),
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=4) as response:
                raw_response = response.read().decode("utf-8")
                try:
                    payload = json.loads(raw_response or "{}")
                except Exception:
                    payload = {}

            # token_strings é lista de strings — sem risco de DetachedInstanceError
            tickets = payload.get("data")
            if isinstance(tickets, dict):
                tickets = [tickets]
            if isinstance(tickets, list):
                for token_str, ticket in zip(token_strings, tickets):
                    if not isinstance(ticket, dict) or ticket.get("status") != "error":
                        continue
                    details = ticket.get("details") if isinstance(ticket.get("details"), dict) else {}
                    error_code = str(details.get("error") or ticket.get("message") or "expo_push_error")
                    if error_code == "DeviceNotRegistered":
                        row = db.query(PushToken).filter(PushToken.expo_push_token == token_str).first()
                        if row:
                            db.delete(row)
                            record_operational_log(
                                db,
                                event_type="push_token_invalidated",
                                severity="warning",
                                source="push_notifications",
                                message="Token Expo removido após retorno de dispositivo inválido.",
                                context={"notification_id": notification_id, "token": token_str, "reason": error_code},
                            )
                    else:
                        record_operational_log(
                            db,
                            event_type="push_failed",
                            severity="warning",
                            source="push_notifications",
                            message=str(ticket.get("message") or error_code),
                            context={"notification_id": notification_id, "type": notification_type, "expo_error": error_code},
                        )
        except Exception as exc:
            LOGGER.warning("push notification skipped notification_id=%s error=%s", notification_id, exc)
            record_operational_exception(
                db,
                event_type="push_failed",
                source="push_notifications",
                exc=exc,
                severity="warning",
                context={"notification_id": notification_id, "type": notification_type},
            )
        db.commit()
    except Exception:
        LOGGER.exception("push worker: falha ao persistir log notification_id=%s", notification_id)
        try:
            db.rollback()
        except Exception:
            pass
    finally:
        db.close()


def _push_notifications_enabled_for(db: Session, notification: Notification) -> bool:
    """Verifica se push_notifications esta habilitado para o tenant da notificacao."""
    from app.services.tenant_plan_service import tenant_feature_enabled  # import local para evitar ciclo
    tenant_id = getattr(notification, "tenant_id", None)
    if not tenant_id:
        # Tenta derivar do user da notificacao
        user_id = getattr(notification, "user_id", None)
        if user_id:
            user = db.get(User, user_id)
            tenant_id = getattr(user, "tenant_id", None) if user else None
    if not tenant_id:
        return True  # indeterminavel → envia (regra: se indeterminavel, envia)
    tenant = db.get(Tenant, tenant_id)
    if not tenant:
        return True
    return tenant_feature_enabled(tenant, db, "push_notifications")


def send_push_for_notification(db: Session, notification: Notification) -> None:
    """Versao sincrona — usada pelo scheduler de retry (que ja tem sua propria sessao)."""
    if not _push_notifications_enabled_for(db, notification):
        return
    if not _should_push(notification):
        return

    token_rows = _tokens_for_notification(db, notification)
    if not token_rows:
        return

    metadata = _metadata(notification)
    messages = [
        {
            "to": token_row.expo_push_token,
            "sound": "default",
            "title": notification.title,
            "body": notification.message,
            "data": {
                "notification_id": notification.id,
                "type": notification.type,
                "related_entity_type": notification.related_entity_type,
                "related_entity_id": notification.related_entity_id,
                **metadata,
            },
        }
        for token_row in token_rows
    ]

    try:
        request = urllib.request.Request(
            EXPO_PUSH_URL,
            data=json.dumps(messages).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=4) as response:
            raw_response = response.read().decode("utf-8")
            try:
                payload = json.loads(raw_response or "{}")
            except Exception:
                payload = {}
            _handle_expo_response(db, notification, token_rows, payload)
    except Exception as exc:
        LOGGER.warning("push notification skipped notification_id=%s error=%s", notification.id, exc)
        record_operational_exception(
            db,
            event_type="push_failed",
            source="push_notifications",
            exc=exc,
            severity="warning",
            context={"notification_id": notification.id, "type": notification.type},
        )


def send_push_for_notification_background(db: Session, notification: Notification, *, session_factory=None) -> None:
    """Versao fire-and-forget — usada pelos routes HTTP.

    Coleta tokens e monta mensagens no thread do request (sessao ainda viva),
    depois despacha o I/O de rede para o _PUSH_EXECUTOR sem bloquear o worker.
    O registro de falha (push_failed) ocorre numa sessao nova criada dentro do
    thread, garantindo que a sessao do request ja fechada nao cause problemas.
    """
    if not _push_notifications_enabled_for(db, notification):
        return
    if not _should_push(notification):
        return

    token_rows = _tokens_for_notification(db, notification)
    if not token_rows:
        return

    metadata = _metadata(notification)
    messages = [
        {
            "to": token_row.expo_push_token,
            "sound": "default",
            "title": notification.title,
            "body": notification.message,
            "data": {
                "notification_id": notification.id,
                "type": notification.type,
                "related_entity_type": notification.related_entity_type,
                "related_entity_id": notification.related_entity_id,
                **metadata,
            },
        }
        for token_row in token_rows
    ]

    # Copia dados escalares ANTES de submeter ao thread — a sessão ORM fecha quando o
    # request termina e acessar atributos de objetos ORM no thread causa DetachedInstanceError.
    notification_id = notification.id
    notification_type = notification.type
    # Extrai expo_push_token (string) de cada ORM row enquanto a sessão ainda está viva.
    token_strings = [str(row.expo_push_token) for row in token_rows]

    _PUSH_EXECUTOR.submit(_do_send, messages, notification_id, notification_type, token_strings, session_factory)
