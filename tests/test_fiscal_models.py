from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.core.database import Base
from app.models.fiscal import TenantFiscalConfig, PaymentProvision

def _db():
    e = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(e, tables=[TenantFiscalConfig.__table__, PaymentProvision.__table__])
    return sessionmaker(bind=e)()

def test_fiscal_config_persists_percent_fields():
    db = _db()
    cfg = TenantFiscalConfig(tenant_id="t1", commission_tax_percent=5, subscription_tax_percent=2, walker_tax_percent=1.5)
    db.add(cfg); db.commit(); db.refresh(cfg)
    assert cfg.id and cfg.active is True
    assert float(cfg.commission_tax_percent) == 5

def test_payment_provision_persists_breakdown():
    db = _db()
    p = PaymentProvision(tenant_id="t1", payment_id="p1", revenue_type="walk_commission",
                         walker_gross=80, walker_tax=0, walker_net=80,
                         platform_gross=20, platform_tax=1, platform_net=19,
                         walker_tax_percent_applied=0, platform_tax_percent_applied=5)
    db.add(p); db.commit(); db.refresh(p)
    assert p.id and float(p.platform_net) == 19
