from __future__ import annotations

import json
import logging
import urllib.request
from typing import Any

from sqlalchemy.orm import Session

from app.models.notification import Notification
from app.models.push_token import PushToken

LOGGER = logging.getLogger("aumigao.push_notifications")
EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"
CRITICAL_NOTIFICATION_TYPES = {
    "new_walk",
    "walker_attempt_created",
    "walker_accepted",
    "walk_completion_review_pending",
    "walk_completion_review_rejected",
    "walk_completion_review_approved",
    "walk_payment_released",
}
CRITICAL_WALK_STATUS_ACTIONS = {"walker_accepted", "ride_in_progress"}


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


def _tokens_for_notification(db: Session, notification: Notification) -> list[str]:
    query = db.query(PushToken)
    if notification.user_id:
        query = query.filter(PushToken.user_id == notification.user_id)
    else:
        return []
    return [row.expo_push_token for row in query.all() if row.expo_push_token]


def send_push_for_notification(db: Session, notification: Notification) -> None:
    if not _should_push(notification):
        return

    tokens = _tokens_for_notification(db, notification)
    if not tokens:
        return

    metadata = _metadata(notification)
    messages = [
        {
            "to": token,
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
        for token in tokens
    ]

    try:
        request = urllib.request.Request(
            EXPO_PUSH_URL,
            data=json.dumps(messages).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=4) as response:
            response.read()
    except Exception as exc:
        LOGGER.warning("push notification skipped notification_id=%s error=%s", notification.id, exc)
