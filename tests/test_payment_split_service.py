"""Testes de unidade para payment_split_service (Sprint 16, Fase A).

Foco: compute_split (clamp da comissão 0-100, platform/walker amounts) e
get_commission_percent (config do tenant vs. padrão).
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.tenant_payment_config import (
    DEFAULT_COMMISSION_PERCENT,
    TenantPaymentConfig,
)
from app.services import payment_split_service as svc


def _db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[TenantPaymentConfig.__table__])
    return sessionmaker(bind=engine)()


def _config(db, *, tenant_id="t1", commission_percent=10.0, active=True):
    cfg = TenantPaymentConfig(
        tenant_id=tenant_id,
        commission_percent=commission_percent,
        active=active,
    )
    db.add(cfg)
    db.commit()
    db.refresh(cfg)
    return cfg


# --------------------------------------------------------------------------
# compute_split
# --------------------------------------------------------------------------

def test_compute_split_happy_path():
    result = svc.compute_split(100.0, 20.0)
    assert result["commission_percent"] == 20.0
    assert result["platform_amount"] == 20.0
    assert result["walker_amount"] == 80.0


def test_compute_split_rounds_to_two_decimals():
    # 33.333% de 100 = 33.333 -> arredonda para 33.33; walker = 66.67
    result = svc.compute_split(100.0, 33.333)
    assert result["platform_amount"] == 33.33
    assert result["walker_amount"] == 66.67
    # soma das partes preserva o valor total arredondado
    assert round(result["platform_amount"] + result["walker_amount"], 2) == 100.0


def test_compute_split_amount_rounded_before_split():
    # amount é arredondado para 2 casas antes do cálculo
    result = svc.compute_split(99.999, 50.0)
    # round(99.999, 2) -> 100.0; metade para cada lado
    assert result["platform_amount"] == 50.0
    assert result["walker_amount"] == 50.0


def test_compute_split_commission_zero():
    result = svc.compute_split(100.0, 0.0)
    assert result["commission_percent"] == 0.0
    assert result["platform_amount"] == 0.0
    assert result["walker_amount"] == 100.0


def test_compute_split_commission_full():
    result = svc.compute_split(100.0, 100.0)
    assert result["commission_percent"] == 100.0
    assert result["platform_amount"] == 100.0
    assert result["walker_amount"] == 0.0


def test_compute_split_clamps_negative_commission_to_zero():
    result = svc.compute_split(100.0, -50.0)
    assert result["commission_percent"] == 0.0
    assert result["platform_amount"] == 0.0
    assert result["walker_amount"] == 100.0


def test_compute_split_clamps_over_100_commission():
    result = svc.compute_split(100.0, 150.0)
    assert result["commission_percent"] == 100.0
    assert result["platform_amount"] == 100.0
    assert result["walker_amount"] == 0.0


def test_compute_split_zero_amount():
    result = svc.compute_split(0.0, 20.0)
    assert result["platform_amount"] == 0.0
    assert result["walker_amount"] == 0.0


def test_compute_split_none_amount_treated_as_zero():
    result = svc.compute_split(None, 20.0)
    assert result["platform_amount"] == 0.0
    assert result["walker_amount"] == 0.0


def test_compute_split_accepts_numeric_string_amount():
    # float("50") funciona; o service faz float(amount or 0)
    result = svc.compute_split("50", 10.0)
    assert result["platform_amount"] == 5.0
    assert result["walker_amount"] == 45.0


def test_compute_split_returns_expected_keys():
    result = svc.compute_split(100.0, 20.0)
    assert set(result.keys()) == {"commission_percent", "platform_amount", "walker_amount"}


# --------------------------------------------------------------------------
# get_commission_percent
# --------------------------------------------------------------------------

def test_get_commission_percent_default_when_no_tenant():
    db = _db()
    assert svc.get_commission_percent(db, None) == DEFAULT_COMMISSION_PERCENT


def test_get_commission_percent_default_when_no_config():
    db = _db()
    assert svc.get_commission_percent(db, "t-sem-config") == DEFAULT_COMMISSION_PERCENT


def test_get_commission_percent_uses_tenant_config():
    db = _db()
    _config(db, tenant_id="t1", commission_percent=12.5)
    assert svc.get_commission_percent(db, "t1") == 12.5


def test_get_commission_percent_ignores_inactive_config():
    db = _db()
    _config(db, tenant_id="t1", commission_percent=5.0, active=False)
    # config inativa é ignorada -> cai no padrão
    assert svc.get_commission_percent(db, "t1") == DEFAULT_COMMISSION_PERCENT


def test_get_commission_percent_zero_config_is_respected():
    # comissão 0% é um valor válido (not None) e deve ser respeitada,
    # não substituída pelo default.
    db = _db()
    _config(db, tenant_id="t1", commission_percent=0.0)
    assert svc.get_commission_percent(db, "t1") == 0.0


def test_get_commission_percent_returns_float():
    db = _db()
    _config(db, tenant_id="t1", commission_percent=15)
    result = svc.get_commission_percent(db, "t1")
    assert isinstance(result, float)
    assert result == 15.0


# --------------------------------------------------------------------------
# build_payment_split (integra get_commission_percent + compute_split)
# --------------------------------------------------------------------------

def test_build_payment_split_uses_tenant_commission():
    db = _db()
    _config(db, tenant_id="t1", commission_percent=30.0)
    result = svc.build_payment_split(db, "t1", 200.0)
    assert result["commission_percent"] == 30.0
    assert result["platform_amount"] == 60.0
    assert result["walker_amount"] == 140.0


def test_build_payment_split_falls_back_to_default():
    db = _db()
    result = svc.build_payment_split(db, None, 100.0)
    assert result["commission_percent"] == DEFAULT_COMMISSION_PERCENT
    assert result["platform_amount"] == 20.0
    assert result["walker_amount"] == 80.0
