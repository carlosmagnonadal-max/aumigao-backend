"""pet_self_walk_service.py — Passeio self-serve do tutor (Perfil Vivo 2.0, Fase D).

Regras de negócio do passeio que o TUTOR registra do próprio cão. NÃO é transação
(sem comissão, sem passeador, sem ledger): é engajamento/dado. Persiste só o
resumo (o mapa vive no cliente).

Efeito colateral do create: um evento na timeline (event_type="self_walk", source
"tutor") com o payload resumo. DECISÃO — evento ÓRFÃO: deletar o self-walk NÃO
apaga o evento. Motivo: a timeline é um jornal append-only (histórico do que
aconteceu — mesmo espírito da carteira, cujo delete não desfaz reminders); o
self_walk é o dado estruturado, o evento é o registro do fato. Apagar o dado não
reescreve a história do pet.

Rate-limit: MAX_SELF_WALKS_PER_DAY por pet por dia (janela = dia UTC de
started_at) — evita spam de score no componente Rotina do wellness.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from uuid import uuid4

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.pet import Pet
from app.models.pet_self_walk import (
    MAX_SELF_WALKS_PER_DAY,
    PetSelfWalk,
)


# ---------------------------------------------------------------------------
# Serialização (contrato JSON fixo)
# ---------------------------------------------------------------------------

def _needs_dict(sw: PetSelfWalk) -> dict:
    return {"pee": sw.need_pee, "poop": sw.need_poop, "water": sw.need_water}


def _behavior_dict(sw: PetSelfWalk) -> dict:
    return {
        "interacted_dogs": sw.interacted_dogs,
        "interacted_people": sw.interacted_people,
        "pulled_leash": sw.pulled_leash,
        "showed_fear": sw.showed_fear,
        "showed_reactivity": sw.showed_reactivity,
    }


def self_walk_dict(sw: PetSelfWalk) -> dict:
    """Serializa um self-walk para o contrato JSON."""
    return {
        "id": sw.id,
        "pet_id": sw.pet_id,
        "started_at": sw.started_at.isoformat() if sw.started_at else None,
        "duration_seconds": sw.duration_seconds,
        # distance_km é Numeric no banco → float no contrato (null-ok).
        "distance_km": float(sw.distance_km) if sw.distance_km is not None else None,
        "walk_type": sw.walk_type,
        "intensity": sw.intensity,
        "had_gps": sw.had_gps,
        "needs": _needs_dict(sw),
        "behavior": _behavior_dict(sw),
        "notes": sw.notes or "",
        "created_at": sw.created_at.isoformat() if sw.created_at else None,
    }


# ---------------------------------------------------------------------------
# Título derivado do evento da timeline
# ---------------------------------------------------------------------------

def _format_duration(seconds: int) -> str:
    """'30min' / '1h05' — formato curto pt-BR para o título do evento."""
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}min"
    hours, rest = divmod(minutes, 60)
    return f"{hours}h{rest:02d}" if rest else f"{hours}h"


def _format_distance(distance_km: float | None) -> str | None:
    """'1,4km' (vírgula decimal pt-BR) ou None se sem distância."""
    if distance_km is None:
        return None
    return f"{distance_km:.1f}".replace(".", ",") + "km"


def timeline_title(duration_seconds: int, distance_km: float | None) -> str:
    """Título derivado: 'Passeio do tutor · 30min · 1,4km' (km opcional)."""
    parts = ["Passeio do tutor", _format_duration(duration_seconds)]
    dist = _format_distance(distance_km)
    if dist:
        parts.append(dist)
    return " · ".join(parts)


# ---------------------------------------------------------------------------
# Rate-limit
# ---------------------------------------------------------------------------

def count_on_day(db: Session, pet_id: str, day: date) -> int:
    """Quantos self-walks já existem para o pet no dia (UTC) de started_at."""
    start = datetime(day.year, day.month, day.day)
    end = start + timedelta(days=1)
    return (
        db.query(func.count(PetSelfWalk.id))
        .filter(
            PetSelfWalk.pet_id == pet_id,
            PetSelfWalk.started_at >= start,
            PetSelfWalk.started_at < end,
        )
        .scalar()
    ) or 0


def day_limit_reached(db: Session, pet_id: str, day: date) -> bool:
    """True se o pet já atingiu o teto diário de self-walks (rate-limit)."""
    return count_on_day(db, pet_id, day) >= MAX_SELF_WALKS_PER_DAY


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def list_self_walks(db: Session, pet_id: str, *, limit: int = 20) -> list[PetSelfWalk]:
    """Self-walks do pet, mais recentes (started_at desc) primeiro."""
    return (
        db.query(PetSelfWalk)
        .filter(PetSelfWalk.pet_id == pet_id)
        .order_by(PetSelfWalk.started_at.desc(), PetSelfWalk.created_at.desc())
        .limit(limit)
        .all()
    )


def create_self_walk(
    db: Session, pet: Pet, tutor_id: str, *,
    started_at: datetime, duration_seconds: int, distance_km: float | None,
    walk_type: str, intensity: str, had_gps: bool,
    needs: dict, behavior: dict, notes: str,
) -> PetSelfWalk:
    """Cria um self-walk + o evento resumo na timeline. Não faz commit (o caller comita).

    O evento de timeline carrega no payload o resumo {duration_seconds, distance_km,
    walk_type, intensity, needs, behavior} — o mesmo que o wellness/telas consomem.
    """
    now = datetime.utcnow()
    sw = PetSelfWalk(
        id=str(uuid4()),
        pet_id=pet.id,
        tutor_id=tutor_id,
        tenant_id=pet.tenant_id,
        started_at=started_at,
        duration_seconds=duration_seconds,
        distance_km=distance_km,
        walk_type=walk_type,
        intensity=intensity,
        had_gps=had_gps,
        need_pee=bool(needs.get("pee")),
        need_poop=bool(needs.get("poop")),
        need_water=bool(needs.get("water")),
        interacted_dogs=bool(behavior.get("interacted_dogs")),
        interacted_people=bool(behavior.get("interacted_people")),
        pulled_leash=bool(behavior.get("pulled_leash")),
        showed_fear=bool(behavior.get("showed_fear")),
        showed_reactivity=bool(behavior.get("showed_reactivity")),
        notes=notes or "",
        created_at=now,
    )
    db.add(sw)
    db.flush()

    _emit_timeline_event(db, pet, sw, created_by_user_id=tutor_id)
    return sw


# event_type usado nos eventos da timeline gerados por passeios do tutor.
EVENT_TYPE_SELF_WALK = "self_walk"


def _emit_timeline_event(
    db: Session, pet: Pet, sw: PetSelfWalk, *, created_by_user_id: str
) -> None:
    """Evento resumo na timeline (source tutor). Import tardio evita ciclo."""
    from app.services.pet_profile_service import record_timeline_event

    distance = float(sw.distance_km) if sw.distance_km is not None else None
    payload = {
        "duration_seconds": sw.duration_seconds,
        "distance_km": distance,
        "walk_type": sw.walk_type,
        "intensity": sw.intensity,
        "needs": _needs_dict(sw),
        "behavior": _behavior_dict(sw),
    }
    record_timeline_event(
        db, pet,
        event_type=EVENT_TYPE_SELF_WALK,
        title=timeline_title(sw.duration_seconds, distance),
        occurred_at=sw.started_at,
        payload_json=json.dumps(payload, ensure_ascii=False),
        source="tutor",
        created_by_user_id=created_by_user_id,
        related_entity_type="pet_self_walk",
        related_entity_id=sw.id,
    )


def delete_self_walk(db: Session, pet_id: str, self_walk_id: str) -> bool:
    """Remove um self-walk do pet. NÃO apaga o evento da timeline (jornal append-only).

    Retorna False se não existe (a rota traduz para 404).
    """
    sw = (
        db.query(PetSelfWalk)
        .filter(PetSelfWalk.id == self_walk_id, PetSelfWalk.pet_id == pet_id)
        .first()
    )
    if not sw:
        return False
    db.delete(sw)
    return True


# ---------------------------------------------------------------------------
# Agregação para o wellness (componente Rotina)
# ---------------------------------------------------------------------------

def count_in_window(
    db: Session, pet_id: str, *, start: datetime, end: datetime
) -> int:
    """Nº de self-walks do pet na janela [start, end] (por started_at).

    Usado pelo pet_wellness_service para somar self-walks aos passeios pagos no
    componente Rotina (com detail discriminado).
    """
    return (
        db.query(func.count(PetSelfWalk.id))
        .filter(
            PetSelfWalk.pet_id == pet_id,
            PetSelfWalk.started_at >= start,
            PetSelfWalk.started_at <= end,
        )
        .scalar()
    ) or 0


