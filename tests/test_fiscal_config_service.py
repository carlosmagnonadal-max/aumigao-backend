from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.core.database import Base
from app.models.fiscal import TenantFiscalConfig
from app.services import fiscal_config_service as svc

def _db():
    e = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(e, tables=[TenantFiscalConfig.__table__])
    return sessionmaker(bind=e)()

def test_resolve_returns_zero_defaults_when_absent():
    db = _db()
    cfg = svc.resolve_fiscal_config(db, "t-x")
    assert float(cfg.commission_tax_percent) == 0
    assert float(cfg.walker_tax_percent) == 0

def test_upsert_creates_then_updates():
    db = _db()
    a = svc.upsert_fiscal_config(db, "t1", {"commission_tax_percent": 5})
    assert float(a.commission_tax_percent) == 5
    b = svc.upsert_fiscal_config(db, "t1", {"commission_tax_percent": 3, "walker_tax_percent": 1})
    assert a.id == b.id and float(b.commission_tax_percent) == 3 and float(b.walker_tax_percent) == 1
    assert db.query(TenantFiscalConfig).count() == 1
