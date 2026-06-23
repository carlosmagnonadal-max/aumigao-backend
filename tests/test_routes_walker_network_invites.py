"""net-T2 — endpoints walker-facing de convites à Rede Aumigão.

GET  /walker/network/invites              -> lista convites pending do walker do token
POST /walker/network/invites/{id}/accept  -> pending -> active
POST /walker/network/invites/{id}/decline -> pending -> declined

Ownership: o walker só vê/responde os próprios convites (deriva do token).
net-T4 — GET /walker/network/me expõe network_access do tenant do walker.
"""
from datetime import datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.core.database import Base, get_db, get_walker_self_db
from app.dependencies.auth import get_current_user
from app.models.tenant import Tenant
from app.models.tenant_walker_access import TenantWalkerAccess
from app.models.user import User
from app.models.walker_profile import WalkerProfile
from app.routes import walker_network
from app.services.tenant_seed_service import DEFAULT_TENANT_SLUG

TENANT_ID = "t-net"
TENANT_B_ID = "t-net-b"
WALKER_ID = "walker-net"
WALKER_B_ID = "walker-net-b"


def build():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(Tenant(id=TENANT_ID, name="Petshop A", slug=DEFAULT_TENANT_SLUG, status="active", plan="business"))
    db.add(Tenant(id=TENANT_B_ID, name="Petshop B", slug="petshop-b", status="active", plan="business"))
    for uid in (WALKER_ID, WALKER_B_ID):
        db.add(User(id=uid, email=f"{uid}@x.com", password_hash="x", role="walker", tenant_id=TENANT_ID, full_name="P"))
        db.add(WalkerProfile(id=f"wp-{uid}", user_id=uid, full_name="P", status="active", active_as_walker=True))
    db.commit()
    test_app = FastAPI()
    test_app.include_router(walker_network.walker_router)
    test_app.dependency_overrides[get_db] = lambda: db
    # Passo 2: GET /walker/network/invites e /me usam get_walker_self_db.
    test_app.dependency_overrides[get_walker_self_db] = lambda: db
    return test_app, db


def as_walker(test_app, db, uid):
    test_app.dependency_overrides[get_current_user] = lambda: db.get(User, uid)
    return TestClient(test_app)


def _invite(db, tenant_id, walker_id, status="pending"):
    access = TenantWalkerAccess(
        tenant_id=tenant_id,
        walker_user_id=walker_id,
        status=status,
        invited_at=datetime.utcnow() if status == "pending" else None,
    )
    db.add(access)
    db.commit()
    db.refresh(access)
    return access


def test_list_returns_only_my_pending_invites():
    test_app, db = build()
    mine = _invite(db, TENANT_ID, WALKER_ID)
    _invite(db, TENANT_B_ID, WALKER_ID, status="active")  # já aceito -> não listar
    _invite(db, TENANT_ID, WALKER_B_ID)  # de outro walker -> não listar
    client = as_walker(test_app, db, WALKER_ID)
    r = client.get("/walker/network/invites")
    assert r.status_code == 200, r.text
    body = r.json()
    assert [x["id"] for x in body] == [mine.id]
    assert body[0]["status"] == "pending"


def test_accept_transitions_pending_to_active():
    test_app, db = build()
    inv = _invite(db, TENANT_ID, WALKER_ID)
    client = as_walker(test_app, db, WALKER_ID)
    r = client.post(f"/walker/network/invites/{inv.id}/accept")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "active"
    db.refresh(inv)
    assert inv.status == "active"
    assert inv.responded_at is not None


def test_decline_transitions_pending_to_declined():
    test_app, db = build()
    inv = _invite(db, TENANT_ID, WALKER_ID)
    client = as_walker(test_app, db, WALKER_ID)
    r = client.post(f"/walker/network/invites/{inv.id}/decline")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "declined"
    db.refresh(inv)
    assert inv.status == "declined"
    assert inv.responded_at is not None


def test_cannot_respond_to_another_walkers_invite():
    test_app, db = build()
    inv = _invite(db, TENANT_ID, WALKER_B_ID)
    client = as_walker(test_app, db, WALKER_ID)
    r = client.post(f"/walker/network/invites/{inv.id}/accept")
    assert r.status_code == 404, r.text
    db.refresh(inv)
    assert inv.status == "pending"


def test_cannot_accept_non_pending_invite():
    test_app, db = build()
    inv = _invite(db, TENANT_ID, WALKER_ID, status="active")
    client = as_walker(test_app, db, WALKER_ID)
    r = client.post(f"/walker/network/invites/{inv.id}/accept")
    assert r.status_code == 409, r.text


def test_me_exposes_network_access_capability():
    test_app, db = build()
    client = as_walker(test_app, db, WALKER_ID)
    r = client.get("/walker/network/me")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "network_access" in body
    assert body["tenant_id"] == TENANT_ID
    # tenant business -> network disponível
    assert body["network_access"] is True
