from sqlalchemy.orm import Session
from app.models.fiscal import TenantFiscalConfig

_EDITABLE = {
    "commission_tax_percent", "subscription_tax_percent", "walker_tax_percent",
    "iss_percent", "municipal_service_code", "simples_nacional", "cnae",
    "service_description", "active",
}

def get_fiscal_config(db: Session, tenant_id: str) -> TenantFiscalConfig | None:
    return db.query(TenantFiscalConfig).filter(TenantFiscalConfig.tenant_id == tenant_id).first()

def resolve_fiscal_config(db: Session, tenant_id: str) -> TenantFiscalConfig:
    """Retorna a config do tenant ou uma instância transitória com defaults zero
    (NÃO persistida) — para o cálculo de provisão nunca falhar por ausência."""
    cfg = get_fiscal_config(db, tenant_id)
    if cfg is not None:
        return cfg
    return TenantFiscalConfig(
        tenant_id=tenant_id, commission_tax_percent=0,
        subscription_tax_percent=0, walker_tax_percent=0,
    )

def upsert_fiscal_config(db: Session, tenant_id: str, values: dict) -> TenantFiscalConfig:
    cfg = get_fiscal_config(db, tenant_id)
    if cfg is None:
        cfg = TenantFiscalConfig(tenant_id=tenant_id)
        db.add(cfg)
    for k, v in values.items():
        if k in _EDITABLE and v is not None:
            setattr(cfg, k, v)
    db.commit()
    db.refresh(cfg)
    return cfg
