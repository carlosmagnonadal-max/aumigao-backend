"""Testes do pet_health_service — status calculado, agregação e briefing (Fase A)."""
from __future__ import annotations

import app.models  # noqa: F401

from datetime import date, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.models.pet import Pet
from app.models.pet_health_record import PetHealthRecord
from app.models.pet_reminder import PetReminder
from app.models.walk_observation import WalkObservation
from app.services import pet_health_service as health


def _db():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    return sessionmaker(bind=eng)()


TODAY = date(2026, 7, 2)


def test_record_status_variants():
    assert health.record_status(None, today=TODAY) == "sem_validade"
    assert health.record_status(TODAY - timedelta(days=1), today=TODAY) == "atrasada"
    assert health.record_status(TODAY, today=TODAY) == "vencendo"          # 0 dias
    assert health.record_status(TODAY + timedelta(days=30), today=TODAY) == "vencendo"
    assert health.record_status(TODAY + timedelta(days=31), today=TODAY) == "em_dia"


def test_aggregate_by_kind_counts():
    records = [
        PetHealthRecord(id="1", pet_id="p", kind="vaccine", name="A", applied_at=TODAY,
                        valid_until=TODAY + timedelta(days=100)),   # em_dia
        PetHealthRecord(id="2", pet_id="p", kind="vaccine", name="B", applied_at=TODAY,
                        valid_until=TODAY - timedelta(days=5)),     # atrasada
        PetHealthRecord(id="3", pet_id="p", kind="dewormer", name="C", applied_at=TODAY,
                        valid_until=None),                          # sem_validade
    ]
    agg = health.aggregate_by_kind(records, today=TODAY)
    assert agg["vaccine"] == {"total": 2, "em_dia": 1, "vencendo": 0, "atrasadas": 1, "sem_validade": 0}
    assert agg["dewormer"] == {"total": 1, "em_dia": 0, "vencendo": 0, "atrasadas": 0, "sem_validade": 1}
    # kinds sem registro ainda aparecem zerados (contrato estável).
    assert agg["flea_tick"]["total"] == 0
    assert agg["treatment"]["total"] == 0


def test_create_vaccine_record_creates_reminder():
    db = _db()
    pet = Pet(id="p1", tutor_id="u1", tenant_id="t1", name="Rex")
    db.add(pet); db.commit()
    future = date.today() + timedelta(days=200)
    rec = health.create_health_record(
        db, pet, kind="vaccine", name="Antirrábica", applied_at=date.today(),
        valid_until=future, notes="", created_by_role="tutor",
    )
    db.commit()
    reminders = db.query(PetReminder).filter(PetReminder.pet_id == "p1").all()
    assert len(reminders) == 1
    assert reminders[0].kind == "vaccine"
    assert reminders[0].due_date == future
    assert reminders[0].source_event_id == rec.id


def test_create_non_vaccine_no_reminder():
    db = _db()
    pet = Pet(id="p1", tutor_id="u1", tenant_id="t1", name="Rex")
    db.add(pet); db.commit()
    health.create_health_record(
        db, pet, kind="dewormer", name="Vermífugo", applied_at=date.today(),
        valid_until=date.today() + timedelta(days=90), notes="", created_by_role="tutor",
    )
    db.commit()
    assert db.query(PetReminder).count() == 0


def test_build_health_card_shape():
    db = _db()
    pet = Pet(id="p1", tutor_id="u1", tenant_id="t1", name="Rex", allergies="frango",
              diet_type="seca", diet_grams_per_meal=120)
    db.add(pet); db.commit()
    health.create_health_record(
        db, pet, kind="vaccine", name="Antirrábica", applied_at=date.today(),
        valid_until=date.today() + timedelta(days=200), notes="", created_by_role="tutor",
    )
    db.commit()
    card = health.build_health_card(db, pet)
    assert card["pet_id"] == "p1"
    assert card["counters"]["vaccine"]["total"] == 1
    assert len(card["records"]) == 1
    assert card["profile"]["allergies"] == "frango"
    assert card["profile"]["diet"]["type"] == "seca"
    assert card["profile"]["diet"]["grams_per_meal"] == 120


def test_build_pet_briefing_operational_only():
    db = _db()
    pet = Pet(id="p1", tutor_id="u1", tenant_id="t1", name="Rex", breed="SRD", size="M",
              pulls_leash=True, afraid_of_noise=True, allergies="frango",
              emergency_contact="119999", vet_name="Dr. Vet", vet_phone="1188",
              diet_type="seca", diet_grams_per_meal=120)
    db.add(pet)
    # Observações dos últimos 90d: 1 reativa + 1 incidente.
    db.add(WalkObservation(id="o1", walk_id="w1", pet_id="p1", tenant_id="t1",
                           walker_user_id="wk", socialization="reactive", incident=True,
                           created_at=datetime.utcnow()))
    # Observação antiga (>90d) — não deve contar.
    db.add(WalkObservation(id="o2", walk_id="w2", pet_id="p1", tenant_id="t1",
                           walker_user_id="wk", socialization="reactive",
                           created_at=datetime.utcnow() - timedelta(days=120)))
    db.commit()
    health.create_health_record(
        db, pet, kind="vaccine", name="Antirrábica", applied_at=date.today(),
        valid_until=date.today() + timedelta(days=200), notes="", created_by_role="tutor",
    )
    db.commit()

    b = health.build_pet_briefing(db, pet)
    assert b["identity"]["name"] == "Rex"
    assert b["temperament"]["pulls_leash"] is True
    assert b["health"]["allergies"] == "frango"
    assert b["diet"]["grams_per_meal"] == 120
    assert b["emergency_contact"] == "119999"
    assert b["vet"]["name"] == "Dr. Vet"
    assert b["observations_summary"]["total"] == 1  # antiga excluída
    assert b["observations_summary"]["reactive_count"] == 1
    assert b["observations_summary"]["incidents"] == 1
    assert b["health_card_status"]["vaccine"]["status"] == "em_dia"
    assert b["health_card_status"]["dewormer"]["status"] == "sem_registro"
    # NÃO vaza dados do tutor / endereço / valores.
    for forbidden in ("tutor_id", "address", "price", "value"):
        assert forbidden not in b
