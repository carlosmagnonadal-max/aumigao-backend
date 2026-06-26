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

def test_summary_aggregates_reserved_and_net():
    db = _db()
    cfg_svc.upsert_fiscal_config(db, "t1", {"commission_tax_percent": 10, "walker_tax_percent": 5})
    svc.compute_and_store_provision(db, "t1", FakePayment("a", 100, 20, 80), "walk_commission")
    svc.compute_and_store_provision(db, "t1", FakePayment("b", 100, 20, 80), "walk_commission")
    s = svc.financial_summary(db, "t1")
    assert s["count"] == 2
    assert round(s["platform_tax_reserved"], 2) == 4.0   # 2 x (20*10%)
    assert round(s["platform_net"], 2) == 36.0
    assert round(s["walker_tax_reserved"], 2) == 8.0     # 2 x (80*5%)
    assert round(s["walker_net"], 2) == 152.0

def test_summary_other_tenant_isolated():
    db = _db()
    svc.compute_and_store_provision(db, "t1", FakePayment("a", 100, 20, 80), "walk_commission")
    s = svc.financial_summary(db, "t2")
    assert s["count"] == 0 and s["platform_net"] == 0
