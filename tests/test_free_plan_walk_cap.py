"""Plano free: cap mensal de passeios próprios (default 40, env FREE_PLAN_WALK_CAP).

REGRA DE CONTAGEM (decisão documentada aqui):
  - Conta passeios do tenant CRIADOS (created_at) no mês corrente em BRT (UTC-3 fixo).
  - EXCLUI passeios com status 'cancelado' (case-insensitive; valor canônico
    'Cancelado') — cancelar devolve a vaga do cap.
  - INCLUI aguardando_pagamento/agendado/concluído (criado e não-cancelado conta).
  - Passeios de meses anteriores NÃO contam (janela = mês corrente BRT).

Enforcement: fronteira de CRIAÇÃO do passeio (create_walk → enforce_free_plan_walk_cap).
Ao atingir o cap → HTTP 403 com mensagem clara de upgrade.
Trial 21d: sem cap (plano efetivo = pro). Pro/enterprise: nunca têm cap.
"""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models import *  # noqa: F403
from app.models.tenant import Tenant
from app.models.walk import Walk
from app.services.tenant_free_plan_service import (
    count_tenant_walks_current_month,
    current_month_window_utc,
    enforce_free_plan_walk_cap,
    free_plan_walk_cap,
)


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    session = Session()
    yield session
    session.close()


def _tenant(db, tid="t-free", plan="free", **kw) -> Tenant:
    t = Tenant(id=tid, name=tid, slug=tid, status="active", plan=plan, **kw)
    db.add(t)
    db.commit()
    return t


def _walk(db, tenant_id, i, status="Agendado", created_at=None):
    db.add(
        Walk(
            id=f"walk-{tenant_id}-{i}",
            tutor_id="tutor-1",
            tenant_id=tenant_id,
            pet_id="pet-1",
            scheduled_date="2099-01-01T10:00:00",
            duration_minutes=30,
            price=30.0,
            status=status,
            created_at=created_at or datetime.utcnow(),
        )
    )
    db.commit()


# ── configuração do cap ─────────────────────────────────────────────────────

def test_default_cap_is_40(monkeypatch):
    monkeypatch.delenv("FREE_PLAN_WALK_CAP", raising=False)
    assert free_plan_walk_cap() == 40


def test_cap_from_env(monkeypatch):
    monkeypatch.setenv("FREE_PLAN_WALK_CAP", "10")
    assert free_plan_walk_cap() == 10


def test_cap_invalid_env_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("FREE_PLAN_WALK_CAP", "abc")
    assert free_plan_walk_cap() == 40
    monkeypatch.setenv("FREE_PLAN_WALK_CAP", "0")
    assert free_plan_walk_cap() == 40  # não desliga o cap por engano de config


# ── janela mensal BRT ───────────────────────────────────────────────────────

def test_month_window_brt():
    # 2026-07-15 12:00 UTC → mês 2026-07 BRT; início = 2026-07-01 00:00 BRT = 03:00 UTC.
    now = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
    start, label = current_month_window_utc(now)
    assert label == "2026-07"
    assert start == datetime(2026, 7, 1, 3, 0)


def test_month_window_brt_edge_utc_month_ahead():
    # 2026-08-01 01:00 UTC ainda é 31/07 22:00 em BRT → mês 2026-07.
    now = datetime(2026, 8, 1, 1, 0, tzinfo=timezone.utc)
    start, label = current_month_window_utc(now)
    assert label == "2026-07"
    assert start == datetime(2026, 7, 1, 3, 0)


# ── contagem ────────────────────────────────────────────────────────────────

def test_count_excludes_cancelled_and_other_months(db):
    _tenant(db)
    _walk(db, "t-free", 1, status="Agendado")
    _walk(db, "t-free", 2, status="Concluído")
    _walk(db, "t-free", 3, status="aguardando_pagamento")
    _walk(db, "t-free", 4, status="Cancelado")  # cancelado NÃO conta
    _walk(db, "t-free", 5, status="cancelado")  # case-insensitive
    _walk(db, "t-free", 6, created_at=datetime.utcnow() - timedelta(days=70))  # mês anterior
    assert count_tenant_walks_current_month(db, "t-free") == 3


def test_count_isolated_per_tenant(db):
    _tenant(db, "t-a")
    _tenant(db, "t-b")
    _walk(db, "t-a", 1)
    _walk(db, "t-b", 1)
    assert count_tenant_walks_current_month(db, "t-a") == 1


# ── enforcement ─────────────────────────────────────────────────────────────

def test_cap_blocks_free_tenant_at_limit(db, monkeypatch):
    monkeypatch.setenv("FREE_PLAN_WALK_CAP", "2")
    t = _tenant(db)
    _walk(db, "t-free", 1)
    _walk(db, "t-free", 2)
    with pytest.raises(HTTPException) as exc:
        enforce_free_plan_walk_cap(db, t)
    assert exc.value.status_code == 403
    assert "plano Pro" in str(exc.value.detail)
    assert "2/2" in str(exc.value.detail)


def test_cap_allows_below_limit(db, monkeypatch):
    monkeypatch.setenv("FREE_PLAN_WALK_CAP", "2")
    t = _tenant(db)
    _walk(db, "t-free", 1)
    enforce_free_plan_walk_cap(db, t)  # 1 < 2 → não levanta


def test_cancelled_walk_frees_cap_slot(db, monkeypatch):
    monkeypatch.setenv("FREE_PLAN_WALK_CAP", "2")
    t = _tenant(db)
    _walk(db, "t-free", 1)
    _walk(db, "t-free", 2, status="Cancelado")
    enforce_free_plan_walk_cap(db, t)  # 1 não-cancelado < 2 → não levanta


def test_cap_ignores_pro_enterprise(db, monkeypatch):
    monkeypatch.setenv("FREE_PLAN_WALK_CAP", "1")
    pro = _tenant(db, "t-pro", "pro")
    ent = _tenant(db, "t-ent", "enterprise")
    _walk(db, "t-pro", 1)
    _walk(db, "t-pro", 2)
    _walk(db, "t-ent", 1)
    enforce_free_plan_walk_cap(db, pro)  # no-op
    enforce_free_plan_walk_cap(db, ent)  # no-op


def test_cap_exempt_during_trial(db, monkeypatch):
    monkeypatch.setenv("FREE_PLAN_WALK_CAP", "1")
    t = _tenant(db, "t-trial", "free", trial_ends_at=datetime.utcnow() + timedelta(days=5))
    _walk(db, "t-trial", 1)
    _walk(db, "t-trial", 2)
    enforce_free_plan_walk_cap(db, t)  # trial ativo → plano efetivo pro → sem cap


def test_cap_applies_after_trial_expires(db, monkeypatch):
    monkeypatch.setenv("FREE_PLAN_WALK_CAP", "1")
    t = _tenant(db, "t-exp", "free", trial_ends_at=datetime.utcnow() - timedelta(days=1))
    _walk(db, "t-exp", 1)
    with pytest.raises(HTTPException):
        enforce_free_plan_walk_cap(db, t)
