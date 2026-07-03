"""pet_health_service.py — Carteira de saúde + ficha rica + briefing (Perfil Vivo 2.0, Fase A).

Separado do pet_profile_service para manter cada arquivo < 500 linhas. Reutiliza:
  - ensure_vaccine_reminder (pet_profile_service) → integra vacina com o PetReminder (Fase 3);
  - _pet_stats (pet_profile route) NÃO é importado aqui; o agregado de observações do
    briefing usa uma consulta enxuta local (últimos 90d), no espírito da Fase 5.

O STATUS de um registro é sempre CALCULADO em runtime a partir de valid_until (nunca
persistido) — evita drift de dados.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from uuid import uuid4

from sqlalchemy.orm import Session

from app.models.pet import Pet
from app.models.pet_health_record import HEALTH_RECORD_KINDS, PetHealthRecord
from app.models.walk_observation import WalkObservation

# Janela (dias) em que uma validade futura é considerada "vencendo".
VENCENDO_WINDOW_DAYS = 30


# ---------------------------------------------------------------------------
# Status calculado
# ---------------------------------------------------------------------------

def record_status(valid_until: date | None, *, today: date | None = None) -> str:
    """Status de UM registro a partir da validade.

    - sem_validade: valid_until é None (tratamento sem vencimento, p.ex.);
    - atrasada: já venceu (valid_until < hoje);
    - vencendo: vence em <= 30 dias (0..30, inclusive hoje);
    - em_dia: vence em > 30 dias.
    """
    if valid_until is None:
        return "sem_validade"
    ref = today or date.today()
    delta = (valid_until - ref).days
    if delta < 0:
        return "atrasada"
    if delta <= VENCENDO_WINDOW_DAYS:
        return "vencendo"
    return "em_dia"


def _empty_counters() -> dict:
    return {"total": 0, "em_dia": 0, "vencendo": 0, "atrasadas": 0, "sem_validade": 0}


def aggregate_by_kind(records: list[PetHealthRecord], *, today: date | None = None) -> dict:
    """Contadores por kind (total/em_dia/vencendo/atrasadas/sem_validade).

    Retorna um dict com uma entrada por kind conhecido, mesmo que zerado — o
    front consome uma estrutura estável (contrato).
    """
    ref = today or date.today()
    out = {kind: _empty_counters() for kind in sorted(HEALTH_RECORD_KINDS)}
    for r in records:
        bucket = out.setdefault(r.kind, _empty_counters())
        bucket["total"] += 1
        status = record_status(r.valid_until, today=ref)
        if status == "atrasada":
            bucket["atrasadas"] += 1
        else:
            bucket[status] += 1
    return out


def record_dict(r: PetHealthRecord, *, today: date | None = None) -> dict:
    """Serializa um registro para o contrato JSON (com status calculado)."""
    return {
        "id": r.id,
        "pet_id": r.pet_id,
        "kind": r.kind,
        "name": r.name,
        "applied_at": r.applied_at.isoformat() if r.applied_at else None,
        "valid_until": r.valid_until.isoformat() if r.valid_until else None,
        "notes": r.notes or "",
        "created_by_role": r.created_by_role,
        "status": record_status(r.valid_until, today=today),
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
    }


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def list_health_records(db: Session, pet_id: str) -> list[PetHealthRecord]:
    """Todos os registros do pet, mais recentes (applied_at desc) primeiro."""
    return (
        db.query(PetHealthRecord)
        .filter(PetHealthRecord.pet_id == pet_id)
        .order_by(PetHealthRecord.applied_at.desc(), PetHealthRecord.created_at.desc())
        .all()
    )


def create_health_record(
    db: Session, pet: Pet, *, kind: str, name: str, applied_at: date,
    valid_until: date | None, notes: str, created_by_role: str,
) -> PetHealthRecord:
    """Cria um registro na carteira. Vacina com validade cria/atualiza o PetReminder.

    Não faz commit — o caller comita. Integra com a Fase 3 (reminders) para
    kind=vaccine com valid_until: reaproveita ensure_vaccine_reminder ligando o
    reminder ao registro (source_event_id = record.id) — idempotente por registro.
    """
    now = datetime.utcnow()
    record = PetHealthRecord(
        id=str(uuid4()),
        pet_id=pet.id,
        tenant_id=pet.tenant_id,
        kind=kind,
        name=name,
        applied_at=applied_at,
        valid_until=valid_until,
        notes=notes or "",
        created_by_role=created_by_role,
        created_at=now,
        updated_at=now,
    )
    db.add(record)
    db.flush()

    # Integração com reminders (Fase 3): vacina com validade futura → reminder de vacina.
    if kind == "vaccine" and valid_until is not None and valid_until > date.today():
        from app.services.pet_profile_service import ensure_vaccine_reminder

        ensure_vaccine_reminder(
            db, pet, due_date=valid_until, source_event_id=record.id, kind="vaccine"
        )

    return record


def delete_health_record(db: Session, pet_id: str, record_id: str) -> bool:
    """Remove um registro do pet. Retorna False se não existe (rota traduz p/ 404)."""
    record = (
        db.query(PetHealthRecord)
        .filter(PetHealthRecord.id == record_id, PetHealthRecord.pet_id == pet_id)
        .first()
    )
    if not record:
        return False
    db.delete(record)
    return True


# ---------------------------------------------------------------------------
# Health card (ficha + carteira agregada)
# ---------------------------------------------------------------------------

def _diet_summary(pet: Pet) -> dict:
    return {
        "type": pet.diet_type,
        "brand": pet.diet_brand,
        "line": pet.diet_line,
        "grams_per_meal": pet.diet_grams_per_meal,
        "meals_per_day": pet.diet_meals_per_day,
        "meal_times": pet.diet_meal_times,
        "notes": pet.diet_notes,
    }


def _profile_fields(pet: Pet) -> dict:
    """Campos da ficha rica (dieta, vet, microchip, alergias/medicações/restrições)."""
    return {
        "microchip": pet.microchip,
        "chip_number": pet.chip_number,
        "vet_name": pet.vet_name,
        "vet_phone": pet.vet_phone,
        "emergency_contact": pet.emergency_contact,
        "allergies": pet.allergies or "",
        "medications": pet.medications or "",
        "restrictions": pet.restrictions or "",
        "health_notes": pet.health_notes or "",
        "diet": _diet_summary(pet),
    }


def build_health_card(db: Session, pet: Pet, *, today: date | None = None) -> dict:
    """Payload do GET /health-card: contadores por kind + registros + ficha."""
    ref = today or date.today()
    records = list_health_records(db, pet.id)
    return {
        "pet_id": pet.id,
        "counters": aggregate_by_kind(records, today=ref),
        "records": [record_dict(r, today=ref) for r in records],
        "profile": _profile_fields(pet),
    }


# ---------------------------------------------------------------------------
# Briefing do passeio (visão do passeador/admin)
# ---------------------------------------------------------------------------

def worst_status_by_kind(records: list[PetHealthRecord], *, today: date) -> dict[str, str]:
    """Pior status por kind — helper CANÔNICO compartilhado pelos serviços.

    Ordem de severidade: atrasada (3) > vencendo (2) > sem_validade (1) > em_dia (0).
    Semântica: em_dia é o MELHOR estado (validade ok, longe de vencer). sem_validade
    é pior que em_dia porque há incerteza sobre quando o tratamento vence/precisará
    ser renovado (ex.: vermífugo sem prazo anotado). A tripla wells/achieve/health
    usa ESTA função — uma única fonte da verdade.
    """
    severity: dict[str, int] = {"atrasada": 3, "vencendo": 2, "sem_validade": 1, "em_dia": 0}
    worst: dict[str, str] = {}
    for r in records:
        st = record_status(r.valid_until, today=today)
        cur = worst.get(r.kind)
        if cur is None or severity.get(st, 0) > severity.get(cur, 0):
            worst[r.kind] = st
    return worst


def _health_card_status_label(records: list[PetHealthRecord], *, today: date | None = None) -> dict:
    """Rótulo agregado enxuto da carteira por kind (ex.: 'vacinas em dia').

    Retorna, por kind: o pior status entre os registros daquele kind
    (atrasada > vencendo > sem_validade > em_dia) + um rótulo curto pt-BR.
    Sem registros → 'sem_registro'.

    NOTA: com a ordem canônica, em_dia + sem_validade → agrega como sem_validade
    (sem_validade é pior = mais alarmante pro passeador). O briefing reflete
    conservadoramente o pior estado observado para aquele kind.
    """
    ref = today or date.today()
    worst_by_kind = worst_status_by_kind(records, today=ref)

    label_map = {
        "atrasada": "atrasada", "vencendo": "vencendo",
        "em_dia": "em dia", "sem_validade": "sem validade",
    }
    kind_pt = {
        "vaccine": "vacinas", "dewormer": "vermífugo",
        "flea_tick": "antipulgas/carrapatos", "treatment": "tratamentos",
    }
    out = {}
    for kind in sorted(HEALTH_RECORD_KINDS):
        st = worst_by_kind.get(kind)
        if st is None:
            out[kind] = {"status": "sem_registro", "label": f"{kind_pt[kind]}: sem registro"}
        else:
            out[kind] = {"status": st, "label": f"{kind_pt[kind]} {label_map[st]}"}
    return out


def _observations_summary(db: Session, pet_id: str, *, now: datetime | None = None) -> dict:
    """Agregado das observações de passeio dos últimos 90d (espírito da Fase 5).

    Contagens úteis pro passeador: total, incidentes, e distribuição de
    socialização (proxy de reatividade) + energia. Não expõe dados do tutor.
    """
    reference = now or datetime.utcnow()
    cutoff = reference - timedelta(days=90)
    rows = (
        db.query(WalkObservation)
        .filter(WalkObservation.pet_id == pet_id, WalkObservation.created_at >= cutoff)
        .all()
    )
    total = len(rows)
    reactive = sum(1 for o in rows if o.socialization == "reactive")
    high_energy = sum(1 for o in rows if o.energy == "high")
    anxious = sum(1 for o in rows if o.mood in {"anxious", "agitated"})
    incidents = sum(1 for o in rows if o.incident)
    return {
        "window_days": 90,
        "total": total,
        "incidents": incidents,
        "reactive_count": reactive,
        "high_energy_count": high_energy,
        "anxious_count": anxious,
    }


def build_pet_briefing(db: Session, pet: Pet, *, now: datetime | None = None) -> dict:
    """Payload operacional do pet para o PASSEADOR (GET /walks/{id}/pet-briefing).

    Só o operacional: identificação, temperamento, saúde operacional, dieta
    resumo, emergência, vet de referência, status agregado da carteira e o
    agregado de observações (90d). NÃO vaza dados do tutor além do que o walker
    já vê no passeio, endereço extra, nem valores.
    """
    reference = now or datetime.utcnow()
    records = list_health_records(db, pet.id)
    return {
        "pet_id": pet.id,
        "identity": {
            "name": pet.name,
            "photo_url": pet.photo_url,
            "breed": pet.breed,
            "size": pet.size,
            "age": pet.age,
            "species": pet.species,
        },
        "temperament": {
            "pulls_leash": pet.pulls_leash,
            "afraid_of_noise": pet.afraid_of_noise,
            "is_social": pet.is_social,
            "can_walk_with_other_pets": pet.can_walk_with_other_pets,
            "behavior_notes": pet.behavior_notes or "",
        },
        "health": {
            "allergies": pet.allergies or "",
            "medications": pet.medications or "",
            "restrictions": pet.restrictions or "",
            "health_notes": pet.health_notes or "",
        },
        "diet": {
            "type": pet.diet_type,
            "grams_per_meal": pet.diet_grams_per_meal,
            "notes": pet.diet_notes,
        },
        "emergency_contact": pet.emergency_contact,
        "vet": {"name": pet.vet_name, "phone": pet.vet_phone},
        "health_card_status": _health_card_status_label(records, today=reference.date()),
        "observations_summary": _observations_summary(db, pet.id, now=reference),
    }
