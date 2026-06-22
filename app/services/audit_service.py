"""Serviço de auditoria (spec §14): helper central para registrar ações críticas.

record_audit_log adiciona o registro à sessão mas NÃO commita — o caller commita
junto com a ação, garantindo atomicidade (ou a auditoria e a ação acontecem, ou
nenhuma). Sanitiza chaves sensíveis e limita o tamanho do payload (spec §14.4).
"""
from __future__ import annotations

import json
from typing import Any

from fastapi import Request
from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.user import User

_SENSITIVE_KEYS = {
    "password", "password_hash", "token", "access_token", "refresh_token",
    "secret", "api_key", "authorization",
    "cpf", "cpf_cnpj", "cpfcnpj", "rg",
}
_MAX_PAYLOAD = 8000


def _sanitize(data: Any) -> Any:
    if not isinstance(data, dict):
        return data
    return {
        k: ("***" if k.lower() in _SENSITIVE_KEYS else _sanitize(v))
        for k, v in data.items()
    }


def _to_json(data: Any) -> str | None:
    if data is None:
        return None
    try:
        return json.dumps(_sanitize(data), default=str, ensure_ascii=False)[:_MAX_PAYLOAD]
    except Exception:
        return None


def record_audit_log(
    db: Session,
    *,
    action: str,
    entity_type: str,
    entity_id: str | None = None,
    actor: User | None = None,
    before: Any = None,
    after: Any = None,
    request: Request | None = None,
    tenant_id: str | None = None,
    actor_type: str = "user",
) -> AuditLog:
    ip_address: str | None = None
    user_agent: str | None = None
    if request is not None:
        try:
            ip_address = request.client.host if request.client else None
            user_agent = request.headers.get("user-agent")
            if tenant_id is None:
                tenant_id = getattr(request.state, "tenant_id", None)
        except Exception:
            pass

    log = AuditLog(
        actor_user_id=getattr(actor, "id", None) if actor is not None else None,
        actor_type=actor_type,
        tenant_id=tenant_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        before_data=_to_json(before),
        after_data=_to_json(after),
        ip_address=ip_address,
        user_agent=(user_agent or "")[:500] or None,
    )
    db.add(log)
    return log
