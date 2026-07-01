from __future__ import annotations

import json
import os
from datetime import datetime
from uuid import uuid4

from sqlalchemy.orm import Session

from app.models.pet import Pet
from app.models.pet_profile_config import PetProfileConfig
from app.models.pet_timeline_event import PetTimelineEvent
from app.models.tenant import Tenant
from app.models.walk import Walk
from app.models.walk_observation import WalkObservation
from app.services.tenant_plan_service import tenant_feature_enabled

PET_PROFILE_FEATURE_KEY = "pet_live_profile"
OBSERVATIONS_FEATURE_KEY = "walk_observations_form"
REMINDERS_FEATURE_KEY = "pet_alerts"


def _env_on(name: str) -> bool:
    return os.getenv(name, "false").lower() in {"1", "true", "yes", "on"}


def get_or_create_pet_profile_config(db: Session, tenant_id: str) -> PetProfileConfig:
    config = db.query(PetProfileConfig).filter(PetProfileConfig.tenant_id == tenant_id).first()
    if not config:
        config = PetProfileConfig(tenant_id=tenant_id)
        db.add(config)
        db.flush()  # flush, não commit — o caller comita
    return config


def _three_layer(tenant: Tenant, db: Session, env_name: str, feature_key: str, config_attr: str) -> bool:
    if not _env_on(env_name):
        return False
    if not tenant_feature_enabled(tenant, db, feature_key):
        return False
    cfg = get_or_create_pet_profile_config(db, tenant.id)
    return bool(getattr(cfg, config_attr))


def pet_profile_active(tenant: Tenant, db: Session) -> bool:
    return _three_layer(tenant, db, "PET_LIVE_PROFILE_ENABLED", PET_PROFILE_FEATURE_KEY, "profile_enabled")


def observations_active(tenant: Tenant, db: Session) -> bool:
    return _three_layer(tenant, db, "WALK_OBSERVATIONS_ENABLED", OBSERVATIONS_FEATURE_KEY, "observations_enabled")


def reminders_active(tenant: Tenant, db: Session) -> bool:
    return _three_layer(tenant, db, "PET_ALERTS_ENABLED", REMINDERS_FEATURE_KEY, "reminders_enabled")


def record_walk_observation(db: Session, walk: Walk, payload: dict) -> WalkObservation:
    """Registra (ou atualiza) a observação estruturada do passeador para um passeio.

    Idempotente por walk_id: se já existe uma WalkObservation para o passeio, faz UPDATE
    dos campos e NÃO cria um segundo PetTimelineEvent.

    Semântica de re-submissão: LAST-WRITE-WINS do formulário INTEIRO — não há merge
    parcial. O cliente deve reenviar TODOS os campos a cada submissão (a rota envia
    sempre o model_dump completo do Pydantic, então campos omitidos no request viram
    None/default e SOBRESCREVEM o valor anterior). incident=False sempre zera
    incident_notes.
    """
    incident = bool(payload.get("incident", False))
    incident_notes = payload.get("incident_notes", "") if incident else ""

    # Busca observação existente
    existing = db.query(WalkObservation).filter(WalkObservation.walk_id == walk.id).first()

    if existing:
        # UPDATE dos campos — não cria novo timeline event
        for field in ("mood", "energy", "socialization", "peed", "pooped"):
            if field in payload:
                setattr(existing, field, payload[field])
        existing.incident = incident
        existing.incident_notes = incident_notes
        db.flush()
        return existing

    # Primeira vez: INSERT + emite timeline event
    obs = WalkObservation(
        walk_id=walk.id,
        pet_id=walk.pet_id,
        tenant_id=walk.tenant_id,
        walker_user_id=payload.get("walker_user_id"),
        mood=payload.get("mood"),
        energy=payload.get("energy"),
        socialization=payload.get("socialization"),
        peed=payload.get("peed"),
        pooped=payload.get("pooped"),
        incident=incident,
        incident_notes=incident_notes,
    )
    db.add(obs)
    db.flush()

    pet = db.get(Pet, walk.pet_id)
    if pet:
        summary = {
            k: payload.get(k)
            for k in ("mood", "energy", "socialization", "peed", "pooped", "incident")
        }
        record_timeline_event(
            db, pet,
            event_type="walk_observation",
            title="Observação do passeio",
            occurred_at=datetime.utcnow(),
            source="walker",
            created_by_user_id=payload.get("walker_user_id"),
            related_entity_type="walk",
            related_entity_id=walk.id,
            payload_json=json.dumps(summary),
        )

    return obs


def record_timeline_event(
    db: Session, pet: Pet, *, event_type: str, title: str, occurred_at: datetime,
    notes: str = "", payload_json: str | None = None, source: str = "tutor",
    created_by_user_id: str | None = None, related_entity_type: str | None = None,
    related_entity_id: str | None = None,
) -> PetTimelineEvent:
    ev = PetTimelineEvent(
        id=str(uuid4()),
        pet_id=pet.id,
        tenant_id=pet.tenant_id,
        event_type=event_type,
        title=title,
        notes=notes,
        payload_json=payload_json,
        occurred_at=occurred_at,
        source=source,
        created_by_user_id=created_by_user_id,
        related_entity_type=related_entity_type,
        related_entity_id=related_entity_id,
    )
    db.add(ev)
    db.flush()
    return ev
