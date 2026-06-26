import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.core.database import Base
from app.models.fiscal import TenantFiscalConfig
from app.schemas.fiscal import FiscalConfigUpdate
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

# --- testes do novo campo tax_regime ---

@pytest.mark.parametrize("regime", ["mei", "simples_nacional", "lucro_presumido", "lucro_real"])
def test_fiscal_config_update_accepts_valid_tax_regime(regime):
    """FiscalConfigUpdate aceita todos os regimes permitidos e None."""
    upd = FiscalConfigUpdate(tax_regime=regime)
    assert upd.tax_regime == regime

def test_fiscal_config_update_accepts_none_tax_regime():
    upd = FiscalConfigUpdate(tax_regime=None)
    assert upd.tax_regime is None

def test_fiscal_config_update_rejects_invalid_tax_regime():
    """FiscalConfigUpdate rejeita regime desconhecido com ValidationError."""
    with pytest.raises(ValidationError):
        FiscalConfigUpdate(tax_regime="lucro_arbitrado")

def test_tax_regime_round_trip_in_model():
    """tax_regime persiste e é lido corretamente (round-trip no ORM)."""
    db = _db()
    cfg = svc.upsert_fiscal_config(db, "t2", {"tax_regime": "lucro_presumido", "commission_tax_percent": 8})
    assert cfg.tax_regime == "lucro_presumido"
    # re-fetch para garantir persistência
    db.expire_all()
    fetched = svc.get_fiscal_config(db, "t2")
    assert fetched is not None
    assert fetched.tax_regime == "lucro_presumido"
