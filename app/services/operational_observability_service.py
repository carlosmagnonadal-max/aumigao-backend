from __future__ import annotations

import json
from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import func, inspect
from sqlalchemy.orm import Session

from app.models.operational_beta_log import OperationalBetaLog

SEVERITY_ORDER = ("critical", "error", "warning", "info")
DEFAULT_COUNTS = {severity: 0 for severity in SEVERITY_ORDER}
DEDUPLICATION_WINDOW_SECONDS = 5 * 60
CONTEXT_IDENTITY_KEYS = (
    "walk_id",
    "review_id",
    "tip_id",
    "payment_id",
    "attempt_id",
    "user_id",
)


def _ensure_operational_log_table(db: Session) -> bool:
    try:
        bind = db.get_bind()
        if not inspect(bind).has_table(OperationalBetaLog.__tablename__):
            OperationalBetaLog.__table__.create(bind=bind, checkfirst=True)
        return True
    except Exception:
        return False


def _normalize_severity(value: str | None) -> str:
    normalized = str(value or "info").strip().lower()
    return normalized if normalized in DEFAULT_COUNTS else "info"


def _serialize_context(context: dict | str | None) -> str | None:
    if context is None:
        return None
    if isinstance(context, str):
        return context[:4000]
    try:
        return json.dumps(context, ensure_ascii=False, default=str, sort_keys=True)[:4000]
    except Exception:
        return json.dumps({"raw": str(context)}, ensure_ascii=False)[:4000]


def _context_identity_from_payload(context: dict | str | None, serialized_context: str | None) -> str:
    try:
        payload = context
        if isinstance(payload, str):
            payload = json.loads(payload)
        if not isinstance(payload, dict) and serialized_context:
            payload = json.loads(serialized_context)
        if isinstance(payload, dict):
            for key in CONTEXT_IDENTITY_KEYS:
                value = payload.get(key)
                if value not in (None, ""):
                    return f"{key}:{value}"
        return serialized_context or ""
    except Exception:
        return serialized_context or ""


def _context_identity_from_log(log: OperationalBetaLog) -> str:
    return _context_identity_from_payload(None, log.context_json)


def _find_recent_duplicate_log(
    db: Session,
    event_type: str,
    severity: str,
    source: str,
    message: str,
    context: dict | str | None,
    serialized_context: str | None,
) -> OperationalBetaLog | None:
    target_identity = _context_identity_from_payload(context, serialized_context)
    try:
        for pending in list(db.new):
            if not isinstance(pending, OperationalBetaLog):
                continue
            if (
                pending.event_type == event_type
                and pending.severity == severity
                and pending.source == source
                and pending.message == message
                and _context_identity_from_log(pending) == target_identity
            ):
                return pending
    except Exception:
        pass

    try:
        cutoff = datetime.utcnow() - timedelta(seconds=DEDUPLICATION_WINDOW_SECONDS)
        candidates = (
            db.query(OperationalBetaLog)
            .filter(
                OperationalBetaLog.event_type == event_type,
                OperationalBetaLog.severity == severity,
                OperationalBetaLog.source == source,
                OperationalBetaLog.message == message,
                OperationalBetaLog.created_at >= cutoff,
            )
            .order_by(OperationalBetaLog.created_at.desc())
            .limit(25)
            .all()
        )
        for candidate in candidates:
            if _context_identity_from_log(candidate) == target_identity:
                return candidate
    except Exception:
        return None
    return None


def record_operational_log(
    db: Session,
    event_type: str,
    severity: str = "info",
    source: str = "backend",
    message: str = "",
    context: dict | str | None = None,
) -> OperationalBetaLog | None:
    try:
        if not _ensure_operational_log_table(db):
            return None

        normalized_event_type = str(event_type or "operational_event")[:120]
        normalized_severity = _normalize_severity(severity)
        normalized_source = str(source or "backend")[:120]
        normalized_message = str(message or "Evento operacional registrado.")[:1000]
        serialized_context = _serialize_context(context)
        duplicate = _find_recent_duplicate_log(
            db,
            normalized_event_type,
            normalized_severity,
            normalized_source,
            normalized_message,
            context,
            serialized_context,
        )
        if duplicate:
            return duplicate

        log = OperationalBetaLog(
            id=str(uuid4()),
            event_type=normalized_event_type,
            severity=normalized_severity,
            source=normalized_source,
            message=normalized_message,
            context_json=serialized_context,
            created_at=datetime.utcnow(),
        )
        db.add(log)
        return log
    except Exception:
        return None


def record_operational_exception(
    db: Session,
    event_type: str,
    source: str,
    exc: Exception,
    context: dict | None = None,
    severity: str = "error",
) -> OperationalBetaLog | None:
    payload = {
        "error_type": exc.__class__.__name__,
        **(context or {}),
    }
    return record_operational_log(
        db,
        event_type=event_type,
        severity=severity,
        source=source,
        message=str(exc) or "Exceção operacional registrada.",
        context=payload,
    )


def _log_payload(log: OperationalBetaLog) -> dict:
    context = None
    if log.context_json:
        try:
            context = json.loads(log.context_json)
        except Exception:
            context = {"raw": log.context_json}

    return {
        "id": log.id,
        "event_type": log.event_type,
        "severity": log.severity,
        "source": log.source,
        "message": log.message,
        "context": context,
        "created_at": log.created_at.isoformat() if log.created_at else None,
    }


def get_operational_observability_snapshot(db: Session, limit: int = 10) -> dict:
    if not _ensure_operational_log_table(db):
        return {
            "items": [],
            "counts_by_severity": DEFAULT_COUNTS.copy(),
            "last_critical": None,
            "data_available": False,
        }

    rows = (
        db.query(OperationalBetaLog)
        .order_by(OperationalBetaLog.created_at.desc())
        .limit(max(1, min(limit, 25)))
        .all()
    )
    counts = DEFAULT_COUNTS.copy()
    for severity, total in db.query(OperationalBetaLog.severity, func.count(OperationalBetaLog.id)).group_by(OperationalBetaLog.severity).all():
        normalized = _normalize_severity(severity)
        counts[normalized] = counts.get(normalized, 0) + int(total or 0)

    last_critical = (
        db.query(OperationalBetaLog)
        .filter(OperationalBetaLog.severity == "critical")
        .order_by(OperationalBetaLog.created_at.desc())
        .first()
    )

    return {
        "items": [_log_payload(row) for row in rows],
        "counts_by_severity": counts,
        "last_critical": _log_payload(last_critical) if last_critical else None,
        "data_available": True,
    }
