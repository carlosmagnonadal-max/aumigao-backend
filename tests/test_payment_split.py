from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.tenant_payment_config import TenantPaymentConfig
from app.services.payment_split_service import compute_split, get_commission_percent


def test_compute_split_default_20():
    s = compute_split(100.0, 20.0)
    assert s["commission_percent"] == 20.0
    assert s["platform_amount"] == 20.0
    assert s["walker_amount"] == 80.0


def test_compute_split_preserves_total():
    s = compute_split(33.33, 20.0)
    assert round(s["platform_amount"] + s["walker_amount"], 2) == 33.33


def test_compute_split_clamps_commission():
    assert compute_split(100, 150)["platform_amount"] == 100.0  # >100 → 100
    assert compute_split(100, -10)["platform_amount"] == 0.0    # <0 → 0


def test_compute_split_zero_amount():
    s = compute_split(0, 20)
    assert s["platform_amount"] == 0.0
    assert s["walker_amount"] == 0.0


def _db():
    engine = create_engine("sqlite:///:memory:")
    TenantPaymentConfig.__table__.create(engine)
    return sessionmaker(bind=engine)()


def test_get_commission_uses_tenant_config():
    db = _db()
    db.add(TenantPaymentConfig(tenant_id="t1", commission_percent=15.0, active=True))
    db.commit()
    assert get_commission_percent(db, "t1") == 15.0


def test_get_commission_falls_back_to_default():
    db = _db()
    assert get_commission_percent(db, "inexistente") == 20.0
    assert get_commission_percent(db, None) == 20.0


def test_inactive_config_uses_default():
    db = _db()
    db.add(TenantPaymentConfig(tenant_id="t2", commission_percent=5.0, active=False))
    db.commit()
    assert get_commission_percent(db, "t2") == 20.0  # config inativa é ignorada
