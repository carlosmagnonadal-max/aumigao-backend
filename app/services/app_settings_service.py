"""Service para configuracoes administrativas persistidas no banco.

Expoe:
  get_setting(db, key, default_dict, tenant_id=None) — read-through com fallback global e depois ao default
  save_setting(db, key, merged_dict, updated_by, tenant_id=None) — upsert serializando JSON
  append_walker_program_action(db, action_type, walker_id, payload)
  recent_walker_program_actions(db, limit=20) — ultimas N acoes, mais recentes primeiro
"""
import json
from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import and_
from sqlalchemy.orm import Session

from app.models.app_setting import AppSetting
from app.models.walker_program_action import WalkerProgramAction


def _merge_dict(base: dict, updates: dict) -> dict:
    """Replica exata da semantica de merge de admin.py (profundo para dicts aninhados)."""
    merged = {**base}
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_dict(merged[key], value)
        else:
            merged[key] = value
    return merged


def _find_row(db: Session, key: str, tenant_id: str | None) -> AppSetting | None:
    """Localiza a linha exata do escopo (tenant ou global)."""
    if tenant_id:
        return (
            db.query(AppSetting)
            .filter(AppSetting.key == key, AppSetting.tenant_id == tenant_id)
            .first()
        )
    # Escopo global: tenant_id IS NULL
    return (
        db.query(AppSetting)
        .filter(AppSetting.key == key, AppSetting.tenant_id.is_(None))
        .first()
    )


def get_setting(db: Session, key: str, default_dict: dict, tenant_id: str | None = None) -> dict:
    """Retorna o setting mesclado: default como base, valor salvo sobrescreve.

    Ordem de lookup:
    1. Linha do tenant (tenant_id fornecido) → prioridade maxima
    2. Linha global (tenant_id IS NULL) → fallback intermediario
    3. default_dict → fallback final

    Numa base vazia (primeira execucao ou pos-deploy limpo) retorna o default
    intacto, preservando o comportamento anterior.
    """
    # Linha do tenant especifico
    row: AppSetting | None = _find_row(db, key, tenant_id) if tenant_id else None
    if row is None and tenant_id:
        # Fallback para linha global
        row = _find_row(db, key, None)
    elif row is None:
        row = _find_row(db, key, None)

    if row is None:
        return dict(default_dict)
    try:
        saved: dict = json.loads(row.value_json or "{}")
    except (json.JSONDecodeError, TypeError):
        saved = {}
    return _merge_dict(default_dict, saved)


def save_setting(
    db: Session,
    key: str,
    merged_dict: dict,
    updated_by: str = "admin",
    tenant_id: str | None = None,
) -> AppSetting:
    """Faz upsert do setting serializado como JSON no escopo dado (tenant ou global)."""
    row: AppSetting | None = _find_row(db, key, tenant_id)
    now = datetime.utcnow()
    if row is None:
        row = AppSetting(
            key=key,
            tenant_id=tenant_id,
            value_json=json.dumps(merged_dict, ensure_ascii=False),
            updated_at=now,
            updated_by=updated_by,
        )
        db.add(row)
    else:
        row.value_json = json.dumps(merged_dict, ensure_ascii=False)
        row.updated_at = now
        row.updated_by = updated_by
    db.commit()
    db.refresh(row)
    return row


def append_walker_program_action(
    db: Session,
    action_type: str,
    walker_id: str | None,
    payload: dict[str, Any],
) -> WalkerProgramAction:
    """Persiste uma nova acao do programa de passeadores."""
    action_id = payload.get("id") or str(uuid4())
    row = WalkerProgramAction(
        id=action_id,
        action_type=action_type,
        walker_id=walker_id,
        payload_json=json.dumps(payload, ensure_ascii=False),
        created_at=datetime.utcnow(),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def recent_walker_program_actions(db: Session, limit: int = 20) -> list[dict[str, Any]]:
    """Retorna as ultimas `limit` acoes, da mais recente para a mais antiga."""
    rows = (
        db.query(WalkerProgramAction)
        .order_by(WalkerProgramAction.created_at.desc())
        .limit(limit)
        .all()
    )
    result = []
    for row in reversed(rows):  # ordem cronologica (antiga -> recente), igual a lista anterior
        try:
            payload = json.loads(row.payload_json or "{}")
        except (json.JSONDecodeError, TypeError):
            payload = {}
        result.append(payload)
    return result
