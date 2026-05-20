from __future__ import annotations

import json
from datetime import datetime
from uuid import uuid4

from sqlalchemy import func, inspect
from sqlalchemy.orm import Session

from app.models.operational_beta_log import OperationalBetaLog

SEVERITY_ORDER = ("critical", "error", "warning", "info")
DEFAULT_COUNTS = {severity: 0 for severity in SEVERITY_ORDER}


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
        return json.dumps(context, ensure_ascii=False, default=str)[:4000]
    except Exception:
        return json.dumps({"raw": str(context)}, ensure_ascii=False)[:4000]


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

        log = OperationalBetaLog(
            id=str(uuid4()),
            event_type=str(event_type or "operational_event")[:120],
            severity=_normalize_severity(severity),
            source=str(source or "backend")[:120],
            message=str(message or "Evento operacional registrado.")[:1000],
            context_json=_serialize_context(context),
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
