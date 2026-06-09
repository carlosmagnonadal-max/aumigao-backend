"""Regras de negócio do Pet Tour (Onda 1 — modalidade especial).

Pet Tour = busca de carro + destino escolhido pelo tutor + duração estendida.
Gated pela feature flag por tenant `pet_tour`. Preço definido pelo tenant
(server-authoritative: o cliente não escolhe o preço).
"""
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.pet_tour import (
    PET_TOUR_FEATURE_KEY,
    TenantPetTourConfig,
)
from app.models.tenant import Tenant
from app.services.tenant_plan_service import enforce_tenant_product_feature, tenant_has_feature

FEATURE_LABEL = "Pet Tour"


def pet_tour_enabled(tenant: Tenant, db: Session) -> bool:
    return tenant_has_feature(tenant, db, PET_TOUR_FEATURE_KEY)


def get_or_create_config(db: Session, tenant_id: str) -> TenantPetTourConfig:
    config = (
        db.query(TenantPetTourConfig)
        .filter(TenantPetTourConfig.tenant_id == tenant_id)
        .first()
    )
    if not config:
        config = TenantPetTourConfig(tenant_id=tenant_id)
        db.add(config)
        db.flush()
    return config


def validate_booking(db: Session, tenant: Tenant, *, destination: str, duration_minutes: int) -> TenantPetTourConfig:
    """Valida um agendamento de Pet Tour e retorna a config (com o preço do tenant)."""
    enforce_tenant_product_feature(tenant, db, PET_TOUR_FEATURE_KEY, FEATURE_LABEL)
    config = get_or_create_config(db, tenant.id)
    if not config.active:
        raise HTTPException(status_code=409, detail="Pet Tour indisponível neste momento.")
    if not (destination or "").strip():
        raise HTTPException(status_code=400, detail="Escolha o destino do Pet Tour.")
    if duration_minutes < config.min_duration_minutes:
        raise HTTPException(
            status_code=400,
            detail=f"O Pet Tour tem duração mínima de {config.min_duration_minutes} minutos.",
        )
    return config
