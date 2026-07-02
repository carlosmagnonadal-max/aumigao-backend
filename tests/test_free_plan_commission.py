"""Plano free: comissão própria 20%, rede N/A, e reverse trial (roda como Pro).

Dinheiro é sensível: garante que free cobra 20% e que NADA muda para pro/enterprise.
"""
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 - registra tabelas no Base.metadata
from app.core.database import Base
from app.models.tenant import Tenant
from app.models.tenant_payment_config import (
    commission_default_for_plan,
    network_commission_default_for_plan,
)
from app.services.payment_split_service import (
    get_commission_percent,
    get_or_create_payment_config,
)
from app.services.tenant_free_plan_service import (
    FREE_PLAN_COMMISSION_PERCENT,
    effective_tenant_plan,
    is_free_plan,
    trial_is_active,
)


def _db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _tenant(db, tid, plan, **kw):
    t = Tenant(id=tid, name=tid, slug=tid, status="active", plan=plan, **kw)
    db.add(t)
    db.commit()
    return t


# ── comissão própria ────────────────────────────────────────────────────────

def test_free_plan_commission_is_20_percent():
    assert commission_default_for_plan("free") == 20.0
    assert FREE_PLAN_COMMISSION_PERCENT == 20.0


def test_free_plan_never_better_than_pro():
    # Escada monotônica de comissão própria: free 20 > pro 10 > enterprise 5.
    assert commission_default_for_plan("free") > commission_default_for_plan("pro")
    assert commission_default_for_plan("pro") > commission_default_for_plan("enterprise")


def test_free_plan_network_commission_is_zero_na():
    # Rede desligada no free → take de rede N/A (0.0). Nunca é cobrado (rede bloqueada),
    # este valor é só coerência.
    assert network_commission_default_for_plan("free") == 0.0
    # pro/enterprise intocados.
    assert network_commission_default_for_plan("pro") == 18.0
    assert network_commission_default_for_plan("enterprise") == 10.0


def test_new_free_config_uses_20():
    db = _db()
    _tenant(db, "t-free", "free")
    assert get_or_create_payment_config(db, "t-free").commission_percent == 20.0


def test_get_commission_percent_free_is_20():
    db = _db()
    _tenant(db, "t-free", "free")
    assert get_commission_percent(db, "t-free") == 20.0


def test_pro_and_enterprise_unchanged():
    db = _db()
    _tenant(db, "t-pro", "pro")
    _tenant(db, "t-ent", "enterprise")
    assert get_or_create_payment_config(db, "t-pro").commission_percent == 10.0
    assert get_or_create_payment_config(db, "t-ent").commission_percent == 5.0


# ── reverse trial: plano efetivo ────────────────────────────────────────────

def test_is_free_plan():
    assert is_free_plan("free") is True
    assert is_free_plan("FREE") is True
    assert is_free_plan("pro") is False
    assert is_free_plan(None) is False


def test_effective_plan_free_without_trial_is_free():
    t = Tenant(id="x", name="x", slug="x", status="active", plan="free")
    assert effective_tenant_plan(t) == "free"
    assert trial_is_active(t) is False


def test_effective_plan_free_with_active_trial_is_pro():
    future = datetime.utcnow() + timedelta(days=10)
    t = Tenant(id="x", name="x", slug="x", status="active", plan="free", trial_ends_at=future)
    assert trial_is_active(t) is True
    assert effective_tenant_plan(t) == "pro"


def test_effective_plan_free_expired_trial_is_free():
    past = datetime.utcnow() - timedelta(days=1)
    t = Tenant(id="x", name="x", slug="x", status="active", plan="free", trial_ends_at=past)
    assert trial_is_active(t) is False
    assert effective_tenant_plan(t) == "free"


def test_effective_plan_pro_tenant_unaffected():
    t = Tenant(id="x", name="x", slug="x", status="active", plan="pro")
    assert effective_tenant_plan(t) == "pro"
