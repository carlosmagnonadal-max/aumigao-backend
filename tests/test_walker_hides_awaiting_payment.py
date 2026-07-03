"""R7 — defesa explícita: o passeador nunca vê passeios 'awaiting_payment'.

Cobre a listagem GET /walks (branch walker_id IS NULL). Passeio aguardando
pagamento fica FORA do pool disponível; passeio agendado (pago/liberado) aparece.
"""
from __future__ import annotations

import app.models  # noqa: F401

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.dependencies.auth import get_current_user
from app.models.pet import Pet
from app.models.tenant import Tenant
from app.models.tenant_walker_access import TenantWalkerAccess
from app.models.user import User
from app.models.walk import Walk
from app.routes import walks
from app.services.tenant_seed_service import DEFAULT_TENANT_SLUG

T1 = "t1"
WALKER = "walker1"
TUTOR = "tutor1"


def _build():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    db.add(Tenant(id=T1, name="T1", slug=DEFAULT_TENANT_SLUG, status="active", plan="business"))
    db.add(User(id=WALKER, email="w@x.com", password_hash="x", role="walker", tenant_id=T1, is_active=True))
    db.add(User(id=TUTOR, email="t@x.com", password_hash="x", role="cliente", tenant_id=T1))
    db.add(Pet(id="p1", tutor_id=TUTOR, tenant_id=T1, name="Rex"))
    db.add(TenantWalkerAccess(id="twa1", tenant_id=T1, walker_user_id=WALKER, status="active"))
    db.commit()

    app_ = FastAPI()
    app_.include_router(walks.router)
    app_.dependency_overrides[get_db] = lambda: db
    app_.dependency_overrides[get_current_user] = lambda: db.get(User, WALKER)
    return TestClient(app_), db


def _walk(db, wid, op_status, status):
    db.add(Walk(id=wid, tutor_id=TUTOR, walker_id=None, tenant_id=T1, pet_id="p1",
                status=status, operational_status=op_status,
                price=50.0, scheduled_date="2026-07-01", duration_minutes=30))
    db.commit()


def test_walker_list_excludes_awaiting_payment():
    client, db = _build()
    _walk(db, "await", "awaiting_payment", "aguardando_pagamento")
    _walk(db, "ready", "pending_walker_confirmation", "Agendado")

    ids = {w["id"] for w in client.get("/walks").json()}
    assert "ready" in ids
    assert "await" not in ids


def test_walker_list_full_excludes_awaiting_payment():
    client, db = _build()
    _walk(db, "await", "awaiting_payment", "aguardando_pagamento")
    _walk(db, "ready", "pending_walker_confirmation", "Agendado")

    ids = {w["id"] for w in client.get("/walks?full=true").json()}
    assert "ready" in ids
    assert "await" not in ids
