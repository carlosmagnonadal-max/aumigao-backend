from __future__ import annotations

import app.models  # noqa: F401

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.dependencies.auth import get_current_user
from app.models.tenant import Tenant, TenantFeature
from app.models.user import User
from app.models.coupon import Coupon
from app.models.walk import Walk
from app.routes import coupons as coupons_routes


def _client():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    # slug="aumigao" makes this the default tenant (resolve_current_tenant fallback)
    db.add(Tenant(id="t1", name="T1", slug="aumigao", status="active", plan="business"))
    db.add(TenantFeature(tenant_id="t1", feature_key="coupons", enabled=True))
    db.add(User(id="u2", email="u2@x.com", password_hash="x", role="tutor", tenant_id="t1"))
    db.add(Walk(id="w1", tenant_id="t1", tutor_id="u2", price=50.0, operational_status="agendado",
                pet_id="p1", scheduled_date="2026-07-01", duration_minutes=30))
    db.add(Coupon(id="c1", tenant_id="t1", code="TREF-R1-RED", discount_type="percent",
                  discount_value=100.0, max_uses=1, max_uses_per_user=1, active=True,
                  is_referral_gift=True))
    db.commit()
    user = db.get(User, "u2")
    application = FastAPI()
    application.include_router(coupons_routes.api_router)
    application.dependency_overrides[get_db] = lambda: db
    application.dependency_overrides[get_current_user] = lambda: user
    return TestClient(application), db


def test_redeem_endpoint_flags_gift_walk():
    client, db = _client()
    resp = client.post("/api/coupons/redeem", json={"code": "TREF-R1-RED", "walk_id": "w1"})
    assert resp.status_code == 200, resp.text
    assert resp.json().get("ok") is True
    assert db.get(Walk, "w1").is_referral_gift is True


def test_redeem_endpoint_without_walk():
    """Redeem without walk_id still succeeds (amount=0, no gift flag)."""
    client, db = _client()
    resp = client.post("/api/coupons/redeem", json={"code": "TREF-R1-RED"})
    assert resp.status_code == 200, resp.text
    assert resp.json().get("ok") is True
