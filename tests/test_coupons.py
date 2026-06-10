from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.dependencies.auth import get_current_user
from app.models.coupon import Coupon, CouponRedemption
from app.models.tenant import Tenant, TenantFeature
from app.models.user import User
from app.routes import coupons as coupons_routes
from app.services import coupon_service as svc
from app.services.tenant_seed_service import DEFAULT_TENANT_SLUG

TENANT_ID = "t1"
USER_ID = "u1"


def _db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[
        Tenant.__table__, TenantFeature.__table__, Coupon.__table__, CouponRedemption.__table__, User.__table__,
    ])
    return sessionmaker(bind=engine)()


def _tenant(db, *, feature=True) -> Tenant:
    t = Tenant(id=TENANT_ID, name="Aumigao", slug=DEFAULT_TENANT_SLUG, status="active", plan="business")
    db.add(t)
    db.add(User(id=USER_ID, email="u@test.com", password_hash="x", role="cliente", tenant_id=TENANT_ID))
    if feature:
        db.add(TenantFeature(tenant_id=TENANT_ID, feature_key="coupons", enabled=True))
    db.commit()
    return t


def _coupon(db, **kw) -> Coupon:
    base = dict(tenant_id=TENANT_ID, code="PROMO10", discount_type="percent", discount_value=10, active=True)
    base.update(kw)
    c = Coupon(**base)
    db.add(c); db.commit(); db.refresh(c)
    return c


# ----- validate -----
def test_validate_invalid_code():
    db = _db(); t = _tenant(db)
    r = svc.validate(db, t, "NOPE", USER_ID, 100)
    assert r["valid"] is False


def test_validate_percent_discount():
    db = _db(); t = _tenant(db); _coupon(db, discount_type="percent", discount_value=10)
    r = svc.validate(db, t, "promo10", USER_ID, 100)  # case-insensitive
    assert r["valid"] and r["discount_amount"] == 10.0 and r["final_amount"] == 90.0


def test_validate_fixed_capped_at_amount():
    db = _db(); t = _tenant(db); _coupon(db, code="R50", discount_type="fixed", discount_value=50)
    r = svc.validate(db, t, "R50", USER_ID, 30)
    assert r["valid"] and r["discount_amount"] == 30.0 and r["final_amount"] == 0.0


def test_validate_min_amount():
    db = _db(); t = _tenant(db); _coupon(db, min_amount=200)
    assert svc.validate(db, t, "PROMO10", USER_ID, 100)["valid"] is False


def test_validate_expired():
    db = _db(); t = _tenant(db); _coupon(db, valid_until=datetime.utcnow() - timedelta(days=1))
    assert svc.validate(db, t, "PROMO10", USER_ID, 100)["valid"] is False


def test_validate_max_uses_exhausted():
    db = _db(); t = _tenant(db); _coupon(db, max_uses=1, uses_count=1)
    assert svc.validate(db, t, "PROMO10", USER_ID, 100)["valid"] is False


def test_validate_per_user_limit():
    db = _db(); t = _tenant(db); c = _coupon(db, max_uses_per_user=1)
    db.add(CouponRedemption(coupon_id=c.id, tenant_id=TENANT_ID, user_id=USER_ID, amount_discounted=10)); db.commit()
    assert svc.validate(db, t, "PROMO10", USER_ID, 100)["valid"] is False


# ----- redeem -----
def test_redeem_blocked_without_feature():
    db = _db(); t = _tenant(db, feature=False); _coupon(db)
    with pytest.raises(HTTPException) as e:
        svc.redeem(db, t, "PROMO10", USER_ID, 100)
    assert e.value.status_code == 403


def test_redeem_records_and_increments():
    db = _db(); t = _tenant(db); _coupon(db)
    red = svc.redeem(db, t, "PROMO10", USER_ID, 100)
    assert red.amount_discounted == 10.0
    assert svc.get_by_code(db, TENANT_ID, "PROMO10").uses_count == 1
    # 2o resgate do mesmo usuário (limite 1) é bloqueado
    with pytest.raises(HTTPException) as e:
        svc.redeem(db, t, "PROMO10", USER_ID, 100)
    assert e.value.status_code == 409


# ----- rota cliente /coupons/validate -----
def _client(db, t):
    app = FastAPI()
    app.include_router(coupons_routes.router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: db.get(User, USER_ID)
    return TestClient(app)


def test_route_validate_gated_off():
    db = _db(); t = _tenant(db, feature=False)
    r = _client(db, t).post("/coupons/validate", json={"code": "X", "amount": 100})
    assert r.status_code == 200 and r.json()["valid"] is False and "indispon" in r.json()["message"].lower()


def test_route_validate_applies_discount():
    db = _db(); t = _tenant(db); _coupon(db, discount_type="percent", discount_value=25)
    r = _client(db, t).post("/coupons/validate", json={"code": "PROMO10", "amount": 80})
    body = r.json()
    assert r.status_code == 200 and body["valid"] is True and body["discount_amount"] == 20.0 and body["final_amount"] == 60.0
