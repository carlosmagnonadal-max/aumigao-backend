"""Testes para provision_service.list_provisions."""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.core.database import Base
from app.models.fiscal import TenantFiscalConfig, PaymentProvision
from app.services import provision_service as svc


class FakePayment:
    def __init__(self, id, amount, platform_amount=None, walker_amount=None):
        self.id = id
        self.amount = amount
        self.platform_amount = platform_amount
        self.walker_amount = walker_amount


def _db():
    e = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(e, tables=[TenantFiscalConfig.__table__, PaymentProvision.__table__])
    return sessionmaker(bind=e)()


def test_list_provisions_orders_newest_first_and_paginates():
    db = _db()
    for i in range(3):
        svc.compute_and_store_provision(db, "t1", FakePayment(f"p{i}", 100, 20, 80), "walk_commission")
    rows = svc.list_provisions(db, "t1", limit=2, offset=0)
    assert len(rows) == 2
    rows2 = svc.list_provisions(db, "t1", limit=2, offset=2)
    assert len(rows2) == 1


def test_list_provisions_filters_by_tenant():
    db = _db()
    svc.compute_and_store_provision(db, "t1", FakePayment("a", 100, 20, 80), "walk_commission")
    svc.compute_and_store_provision(db, "t2", FakePayment("b", 100, 20, 80), "walk_commission")
    assert len(svc.list_provisions(db, "t1", limit=50, offset=0)) == 1
