"""net-T1 — máquina de estados de convite em TenantWalkerAccess.

Estados: pending (convidado), active (aceitou), declined (recusou), revoked (opc).
Colunas novas: invited_at, responded_at. Migration 0032 idempotente.
"""
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 - registra todas as tabelas no Base.metadata
from app.core.database import Base
from app.models.tenant_walker_access import TenantWalkerAccess
from app.schemas.walker_network import (
    TENANT_WALKER_ACCESS_STATUSES,
    TenantWalkerAccessResponse,
)


def _db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine)()


def test_invite_states_present_in_allowed_set():
    for state in ("pending", "active", "declined", "revoked"):
        assert state in TENANT_WALKER_ACCESS_STATUSES


def test_new_invite_columns_exist_on_model():
    engine, _ = _db()
    cols = {c["name"] for c in inspect(engine).get_columns("tenant_walker_access")}
    assert "invited_at" in cols
    assert "responded_at" in cols


def test_can_persist_pending_then_active_with_timestamps():
    _, db = _db()
    from datetime import datetime

    access = TenantWalkerAccess(
        tenant_id="t1",
        walker_user_id="w1",
        status="pending",
        invited_at=datetime.utcnow(),
    )
    db.add(access)
    db.commit()
    db.refresh(access)
    assert access.status == "pending"
    assert access.invited_at is not None
    assert access.responded_at is None

    access.status = "active"
    access.responded_at = datetime.utcnow()
    db.commit()
    db.refresh(access)
    assert access.status == "active"
    assert access.responded_at is not None


def test_response_schema_exposes_invite_fields():
    fields = TenantWalkerAccessResponse.model_fields
    assert "status" in fields
    assert "invited_at" in fields
    assert "responded_at" in fields
