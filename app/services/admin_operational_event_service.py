import json
from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from app.models.admin_operational_event import AdminOperationalEvent
from app.models.user import User


def _json_dump(value: Any) -> str:
    try:
        return json.dumps(value or {}, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return "{}"


def _json_load(value: str | None) -> dict:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


def record_admin_operational_event(
    db: Session,
    *,
    event_type: str,
    entity_type: str,
    entity_id: str,
    title: str,
    description: str = "",
    severity: str = "info",
    actor: User | None = None,
    source: str = "admin-web",
    metadata: dict | None = None,
) -> AdminOperationalEvent:
    event = AdminOperationalEvent(
        id=str(uuid4()),
        event_type=event_type,
        entity_type=entity_type,
        entity_id=entity_id,
        severity=severity,
        title=title,
        description=description,
        actor_user_id=actor.id if actor else None,
        actor_email=actor.email if actor else None,
        source=source,
        metadata_json=_json_dump(metadata),
        created_at=datetime.utcnow(),
    )
    db.add(event)
    return event


def serialize_admin_operational_event(event: AdminOperationalEvent) -> dict:
    return {
        "id": event.id,
        "event_type": event.event_type,
        "entity_type": event.entity_type,
        "entity_id": event.entity_id,
        "severity": event.severity,
        "title": event.title,
        "description": event.description,
        "actor_user_id": event.actor_user_id,
        "actor_email": event.actor_email,
        "source": event.source,
        "metadata": _json_load(event.metadata_json),
        "created_at": event.created_at.isoformat() if event.created_at else None,
    }
