from __future__ import annotations

import json
import os
from datetime import date, datetime
from uuid import uuid4

from sqlalchemy.orm import Session

from app.models.pet import Pet
from app.models.pet_profile_config import PetProfileConfig
from app.models.pet_reminder import PetReminder
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


SHARE_FEATURE_KEY = "pet_share"


def share_active(tenant: Tenant, db: Session) -> bool:
    """Gate de 3 camadas para o compartilhamento público do perfil do pet (Fase 4).

    Env PET_SHARE_ENABLED + TenantFeature pet_share + config.share_enabled.
    Dormente por padrão (todas as camadas default-OFF).
    """
    return _three_layer(tenant, db, "PET_SHARE_ENABLED", SHARE_FEATURE_KEY, "share_enabled")


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


def ensure_vaccine_reminder(
    db: Session, pet: Pet, due_date: date,
    source_event_id: str | None = None, kind: str = "vaccine",
) -> PetReminder:
    """Upsert de PetReminder de vacina/vermífugo ligado a um evento da timeline.

    Idempotência por (pet_id, kind, source_event_id): se já existe um reminder ativo
    com o mesmo source_event_id, atualiza due_date; caso contrário cria um novo.
    """
    q = db.query(PetReminder).filter(
        PetReminder.pet_id == pet.id,
        PetReminder.kind == kind,
        PetReminder.active == True,  # noqa: E712
    )
    if source_event_id:
        q = q.filter(PetReminder.source_event_id == source_event_id)
    existing = q.first()
    if existing:
        if existing.due_date != due_date:
            existing.due_date = due_date
        db.flush()
        return existing
    reminder = PetReminder(
        id=str(uuid4()),
        pet_id=pet.id,
        tenant_id=pet.tenant_id,
        kind=kind,
        due_date=due_date,
        active=True,
        source_event_id=source_event_id,
        created_at=datetime.utcnow(),
    )
    db.add(reminder)
    db.flush()
    return reminder


def build_diary_entry(*, text: str, mood: str | None, title: str | None) -> tuple[str, str]:
    """Monta (título, payload_json) de uma entrada de DIÁRIO do tutor (Fase B).

    O payload é construído no servidor a partir de campos já validados (o payload
    cru do cliente é ignorado): texto obrigatório + humor opcional. Título usa o
    informado ou deriva do texto (truncado). Retorna (title, payload_json).
    """
    text = text.strip()
    diary: dict[str, str] = {"text": text}
    if mood:
        diary["mood"] = mood
    clean_title = (title or "").strip()
    if clean_title:
        diary["title"] = clean_title
        final_title = clean_title
    else:
        final_title = (text[:60] + "…") if len(text) > 60 else text
    return final_title, json.dumps(diary, ensure_ascii=False)


def build_tenant_note(*, context: str, category: str, text: str, title: str | None) -> tuple[str, str]:
    """Monta (título, payload_json) da observação estruturada do TENANT (Fase E).

    Igual ao diary: o payload é construído no servidor a partir de campos já
    validados (o payload cru do cliente é ignorado). Texto obrigatório (<=2000);
    contexto/categoria validados na rota. Título usa o informado ou deriva do texto.
    Retorna (title, payload_json).
    """
    text = text.strip()
    note: dict[str, str] = {"context": context, "category": category, "text": text}
    clean_title = (title or "").strip()
    if clean_title:
        note["title"] = clean_title
        final_title = clean_title
    else:
        final_title = (text[:60] + "…") if len(text) > 60 else text
    return final_title, json.dumps(note, ensure_ascii=False)


def notify_owner_of_tenant_note(db: Session, pet: Pet, *, category: str, text: str) -> None:
    """Notifica o TUTOR dono do pet quando o tenant registra incidente/restrição.

    Best-effort (padrão maybe_downgrade_expired_trial): nunca quebra o request.
    Não faz commit — o caller comita.
    """
    from app.models.pet_timeline_event import TENANT_NOTE_ALERT_CATEGORIES

    if category not in TENANT_NOTE_ALERT_CATEGORIES:
        return
    try:
        from app.routes.notifications import NotificationCreate, _create_notification

        label = "Incidente registrado" if category == "incidente" else "Restrição registrada"
        snippet = (text or "").strip()
        if len(snippet) > 140:
            snippet = snippet[:140] + "…"
        _create_notification(
            db,
            NotificationCreate(
                tenant_id=pet.tenant_id,
                user_id=pet.tutor_id,
                user_role="tutor",
                title=f"{label} para {pet.name}",
                message=snippet or f"A equipe registrou uma observação sobre {pet.name}.",
                type="warning",
                related_entity_type="pet",
                related_entity_id=pet.id,
                metadata={"event": "tenant_note", "category": category},
            ),
        )
    except Exception:  # noqa: BLE001 — notificação best-effort
        pass


def list_pet_companions(db: Session, pet: Pet) -> list[dict]:
    """Mapa de convivência: pets que dividiram shared walk CONCLUÍDO com este pet.

    "Concluído" = shared walk em status confirmed/matched (todos pagaram → o passeio
    aconteceu) — o modelo SharedWalk não tem estado "completed" dedicado, então
    confirmed/matched são os estados terminais positivos (ver desenho da Fase E).
    Conta só participantes que efetivamente pagaram (PARTICIPANT_PAID), excluindo
    declined/cancelled. Mesmo tenant apenas (shared walks são tenant-scoped).

    SANITIZAÇÃO: devolve só pet_id/name/photo_url/breed do OUTRO pet + agregados
    (walks_together, last_walk_at) — NUNCA tutor/endereço/contato. Ordenado por
    walks_together desc, depois last_walk_at desc.
    """
    from app.models.shared_walk import (
        PARTICIPANT_PAID,
        SHARED_CONFIRMED,
        SHARED_MATCHED,
        SharedWalk,
        SharedWalkParticipant,
    )

    concluded = {SHARED_CONFIRMED, SHARED_MATCHED}

    # Shared walks CONCLUÍDOS do tenant do pet em que este pet participou (pago).
    my_walk_ids = [
        row[0]
        for row in (
            db.query(SharedWalkParticipant.shared_walk_id)
            .join(SharedWalk, SharedWalk.id == SharedWalkParticipant.shared_walk_id)
            .filter(
                SharedWalkParticipant.pet_id == pet.id,
                SharedWalkParticipant.status == PARTICIPANT_PAID,
                SharedWalk.tenant_id == pet.tenant_id,
                SharedWalk.status.in_(list(concluded)),
            )
            .all()
        )
    ]
    if not my_walk_ids:
        return []

    # Companheiros: outros pets pagantes nesses mesmos walks (exclui o próprio pet).
    rows = (
        db.query(SharedWalkParticipant.pet_id, SharedWalk.confirmed_at, SharedWalk.created_at)
        .join(SharedWalk, SharedWalk.id == SharedWalkParticipant.shared_walk_id)
        .filter(
            SharedWalkParticipant.shared_walk_id.in_(my_walk_ids),
            SharedWalkParticipant.pet_id != pet.id,
            SharedWalkParticipant.status == PARTICIPANT_PAID,
        )
        .all()
    )

    agg: dict[str, dict] = {}
    for other_pet_id, confirmed_at, created_at in rows:
        when = confirmed_at or created_at
        entry = agg.setdefault(other_pet_id, {"count": 0, "last": None})
        entry["count"] += 1
        if when and (entry["last"] is None or when > entry["last"]):
            entry["last"] = when

    if not agg:
        return []

    # Busca os pets companheiros (mesmo tenant) e monta a saída sanitizada.
    pets = (
        db.query(Pet)
        .filter(Pet.id.in_(list(agg.keys())), Pet.tenant_id == pet.tenant_id)
        .all()
    )
    companions = []
    for other in pets:
        info = agg[other.id]
        companions.append({
            "pet_id": other.id,
            "name": other.name,
            "photo_url": other.photo_url,
            "breed": other.breed or "",
            "walks_together": info["count"],
            "last_walk_at": info["last"].isoformat() if info["last"] else None,
        })
    companions.sort(
        key=lambda c: (c["walks_together"], c["last_walk_at"] or ""),
        reverse=True,
    )
    return companions


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
