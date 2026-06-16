"""R4 — cotação por tenant: preço do passeio, desconto de plano e total.

Decisão Carlos (2026-06-16): taxa de serviço R$5 REMOVIDA (quote sem service_fee);
desconto de plano = % por tenant configurável no admin. total = walk_price - plan_discount.
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 - registra tabelas no Base.metadata
from app.core.database import Base
from app.models.tenant_payment_config import TenantPaymentConfig
from app.services.payment_split_service import compute_quote, get_plan_discount_percent


def _db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


# --------------------------------------------------------------------------
# compute_quote (puro)
# --------------------------------------------------------------------------

def test_compute_quote_no_discount():
    q = compute_quote(100.0, 0.0)
    assert q["walk_price"] == 100.0
    assert q["plan_discount_percent"] == 0.0
    assert q["plan_discount"] == 0.0
    assert q["total"] == 100.0


def test_compute_quote_with_discount():
    q = compute_quote(100.0, 10.0)
    assert q["plan_discount"] == 10.0
    assert q["total"] == 90.0


def test_compute_quote_rounds_to_cents():
    q = compute_quote(99.99, 10.0)
    assert q["plan_discount"] == 10.0  # round(9.999, 2)
    assert q["total"] == 89.99
    # consistência: total + desconto = preço
    assert round(q["total"] + q["plan_discount"], 2) == 99.99


def test_compute_quote_clamps_discount_to_100():
    q = compute_quote(100.0, 150.0)
    assert q["plan_discount_percent"] == 100.0
    assert q["plan_discount"] == 100.0
    assert q["total"] == 0.0


def test_compute_quote_clamps_negative_discount_to_zero():
    q = compute_quote(100.0, -20.0)
    assert q["plan_discount_percent"] == 0.0
    assert q["total"] == 100.0


def test_compute_quote_has_no_service_fee_key():
    # Taxa R$5 removida: o quote não deve carregar service_fee.
    q = compute_quote(100.0, 0.0)
    assert "service_fee" not in q


# --------------------------------------------------------------------------
# get_plan_discount_percent
# --------------------------------------------------------------------------

def test_get_plan_discount_percent_default_zero_without_config():
    db = _db()
    assert get_plan_discount_percent(db, "t-sem-config") == 0.0
    assert get_plan_discount_percent(db, None) == 0.0


def test_get_plan_discount_percent_from_config():
    db = _db()
    db.add(TenantPaymentConfig(tenant_id="t1", plan_discount_percent=15.0, active=True))
    db.commit()
    assert get_plan_discount_percent(db, "t1") == 15.0


def test_get_plan_discount_percent_ignores_inactive_config():
    db = _db()
    db.add(TenantPaymentConfig(tenant_id="t2", plan_discount_percent=15.0, active=False))
    db.commit()
    assert get_plan_discount_percent(db, "t2") == 0.0
