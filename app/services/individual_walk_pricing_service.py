"""Regras do preço de passeio individual por tenant (white label).

Espelha `shared_walk_service.get_or_create_config`: garante uma linha de
configuração por tenant, criando com os defaults do modelo quando ausente.
"""
from sqlalchemy.orm import Session

from app.models.individual_walk_pricing import TenantIndividualWalkPricing


def get_or_create_config(db: Session, tenant_id: str) -> TenantIndividualWalkPricing:
    config = (
        db.query(TenantIndividualWalkPricing)
        .filter(TenantIndividualWalkPricing.tenant_id == tenant_id)
        .first()
    )
    if not config:
        config = TenantIndividualWalkPricing(tenant_id=tenant_id)
        db.add(config)
        db.flush()
    return config
