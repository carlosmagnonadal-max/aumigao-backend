from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.core.database import Base
from app.models.fiscal import TenantFiscalConfig, PaymentProvision
from app.services import fiscal_config_service as cfg_svc
from app.services import provision_service as svc

class FakePayment:
    def __init__(self, id, amount, platform_amount=None, walker_amount=None):
        self.id = id; self.amount = amount
        self.platform_amount = platform_amount; self.walker_amount = walker_amount

def _db():
    e = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(e, tables=[TenantFiscalConfig.__table__, PaymentProvision.__table__])
    return sessionmaker(bind=e)()

def test_walk_commission_uses_commission_rate():
    db = _db()
    cfg_svc.upsert_fiscal_config(db, "t1", {"commission_tax_percent": 5, "walker_tax_percent": 1})
    pay = FakePayment("p1", 100, platform_amount=20, walker_amount=80)
    prov = svc.compute_and_store_provision(db, "t1", pay, "walk_commission")
    assert float(prov.platform_tax) == 1.0 and float(prov.platform_net) == 19.0
    assert float(prov.walker_tax) == 0.8 and float(prov.walker_net) == 79.2

def test_zero_config_yields_zero_tax():
    db = _db()
    pay = FakePayment("p2", 100, platform_amount=20, walker_amount=80)
    prov = svc.compute_and_store_provision(db, "t1", pay, "walk_commission")
    assert float(prov.platform_tax) == 0 and float(prov.walker_tax) == 0

def test_idempotent_same_payment():
    db = _db()
    pay = FakePayment("p3", 100, platform_amount=20, walker_amount=80)
    a = svc.compute_and_store_provision(db, "t1", pay, "walk_commission")
    b = svc.compute_and_store_provision(db, "t1", pay, "walk_commission")
    assert a.id == b.id and db.query(PaymentProvision).count() == 1

def test_immutable_after_rate_change():
    db = _db()
    cfg_svc.upsert_fiscal_config(db, "t1", {"commission_tax_percent": 5})
    pay = FakePayment("p4", 100, platform_amount=20, walker_amount=80)
    svc.compute_and_store_provision(db, "t1", pay, "walk_commission")
    cfg_svc.upsert_fiscal_config(db, "t1", {"commission_tax_percent": 50})
    again = svc.compute_and_store_provision(db, "t1", pay, "walk_commission")
    assert float(again.platform_tax) == 1.0  # congelado na 1ª alíquota

def test_saas_subscription_taxes_full_amount_on_platform():
    db = _db()
    cfg_svc.upsert_fiscal_config(db, "t1", {"subscription_tax_percent": 10})
    pay = FakePayment("p5", 129.90)
    prov = svc.compute_and_store_provision(db, "t1", pay, "saas_subscription")
    assert float(prov.platform_gross) == 129.90 and float(prov.walker_gross) == 0
    assert round(float(prov.platform_tax), 2) == 12.99

def test_tip_taxes_walker_side():
    db = _db()
    cfg_svc.upsert_fiscal_config(db, "t1", {"walker_tax_percent": 10})
    pay = FakePayment("p6", 50)
    prov = svc.compute_and_store_provision(db, "t1", pay, "tip")
    assert float(prov.walker_gross) == 50 and float(prov.platform_gross) == 0
    assert float(prov.walker_tax) == 5.0
