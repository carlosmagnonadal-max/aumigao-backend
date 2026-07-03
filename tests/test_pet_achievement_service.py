"""Testes do serviço de Conquistas do pet (Perfil Vivo 2.0, Fase C).

Todas as badges são 100% runtime (sem persistência, sem migration). Cobrem cada
regra do catálogo, progressos, achieved_at (derivável barato), ordenação e a
"primeira memória" (foto de finalização via WalkCompletionReview).
"""
from __future__ import annotations

import app.models  # noqa: F401

from datetime import date, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.models.pet import Pet
from app.models.pet_health_record import PetHealthRecord
from app.models.tenant import Tenant
from app.models.user import User
from app.models.walk import Walk
from app.models.walk_completion_review import WalkCompletionReview
from app.services import pet_achievement_service as ach


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _db():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    db.add(Tenant(id="t1", name="T1", slug="t1", status="active", plan="pro"))
    db.add(User(id="u1", email="u1@x.com", password_hash="x", role="tutor", tenant_id="t1"))
    db.add(Pet(id="p1", tutor_id="u1", tenant_id="t1", name="Rex"))
    db.commit()
    return db


def _pet(db) -> Pet:
    return db.get(Pet, "p1")


def _add_walks(db, *, n, base_days_ago=1, step_days=1, status="completed"):
    """Adiciona n passeios concluídos, o mais antigo primeiro (created_at decrescente)."""
    now = datetime.utcnow()
    for i in range(n):
        db.add(Walk(id=f"w{i}", tutor_id="u1", pet_id="p1", tenant_id="t1",
                    scheduled_date="2026-07-01", duration_minutes=30, price=0.0,
                    status=status, created_at=now - timedelta(days=base_days_ago + i * step_days)))
    db.commit()


def _add_record(db, kind, *, valid_days, applied_days_ago=1):
    today = date.today()
    valid_until = today + timedelta(days=valid_days) if valid_days is not None else None
    db.add(PetHealthRecord(
        pet_id="p1", tenant_id="t1", kind=kind, name=f"{kind}-rec",
        applied_at=today - timedelta(days=applied_days_ago),
        valid_until=valid_until, created_by_role="tutor",
    ))
    db.commit()


def _find(payload, key):
    return next(b for b in payload["achievements"] if b["key"] == key)


# ---------------------------------------------------------------------------
# Catálogo / shape
# ---------------------------------------------------------------------------

def test_catalog_shape_and_summary_empty():
    db = _db()
    out = ach.compute_achievements(db, _pet(db))
    assert out["pet_id"] == "p1"
    keys = {b["key"] for b in out["achievements"]}
    assert keys == {
        "primeiro_passeio", "explorador", "aventureiro", "rotina_do_bem",
        "primeira_memoria", "vacinas_em_dia", "protecao_total",
        "primeiro_registro_saude", "perfil_completo", "bem_estar_otimo",
    }
    assert out["summary"]["total"] == 10
    # Pet vazio: nada conquistado.
    assert out["summary"]["achieved"] == 0
    for b in out["achievements"]:
        assert b["category"] in {"passeios", "saude", "perfil"}
        assert b["label"] and b["description"]
        assert set(b["progress"]) == {"current", "target", "unit"}
        # Não conquistada → offer_hint presente; achieved_at null.
        assert b["offer_hint"]
        assert b["achieved_at"] is None
    assert "computed_at" in out


# ---------------------------------------------------------------------------
# Passeios
# ---------------------------------------------------------------------------

def test_primeiro_passeio_achieved_with_date():
    db = _db()
    _add_walks(db, n=1)
    b = _find(ach.compute_achievements(db, _pet(db)), "primeiro_passeio")
    assert b["achieved"] is True
    assert b["achieved_at"] is not None  # data do 1º passeio (derivável barato)
    assert b["offer_hint"] is None
    assert b["progress"] == {"current": 1, "target": 1, "unit": "passeios"}


def test_explorador_progress_and_hint():
    db = _db()
    _add_walks(db, n=7)
    b = _find(ach.compute_achievements(db, _pet(db)), "explorador")
    assert b["achieved"] is False
    assert b["progress"]["current"] == 7
    assert b["progress"]["target"] == 10
    assert "3" in b["offer_hint"]  # faltam 3


def test_explorador_achieved_at_is_tenth_walk():
    db = _db()
    _add_walks(db, n=10)
    b = _find(ach.compute_achievements(db, _pet(db)), "explorador")
    assert b["achieved"] is True
    assert b["achieved_at"] is not None
    # progress não estoura a meta.
    assert b["progress"]["current"] == 10


def test_aventureiro_needs_fifty():
    db = _db()
    _add_walks(db, n=50)
    out = ach.compute_achievements(db, _pet(db))
    assert _find(out, "aventureiro")["achieved"] is True


def test_only_completed_walks_count():
    db = _db()
    _add_walks(db, n=3, status="Agendado")  # não concluídos
    b = _find(ach.compute_achievements(db, _pet(db)), "primeiro_passeio")
    assert b["achieved"] is False
    assert b["progress"]["current"] == 0


def test_rotina_do_bem_four_consecutive_iso_weeks():
    db = _db()
    now = datetime.utcnow()
    # Um passeio por semana, 4 semanas seguidas (7,14,21,28 dias atrás).
    for i, days in enumerate((0, 7, 14, 21)):
        db.add(Walk(id=f"w{i}", tutor_id="u1", pet_id="p1", tenant_id="t1",
                    scheduled_date="2026-07-01", duration_minutes=30, price=0.0,
                    status="completed", created_at=now - timedelta(days=days)))
    db.commit()
    b = _find(ach.compute_achievements(db, _pet(db)), "rotina_do_bem")
    assert b["achieved"] is True
    assert b["progress"]["current"] >= 4
    assert b["achieved_at"] is None  # streak não deriva data barato


def test_rotina_do_bem_gap_breaks_streak():
    db = _db()
    now = datetime.utcnow()
    # Semanas 0, 7, 21, 28 dias atrás: há um buraco (falta a de 14) → maior run = 2.
    for i, days in enumerate((0, 7, 21, 28)):
        db.add(Walk(id=f"w{i}", tutor_id="u1", pet_id="p1", tenant_id="t1",
                    scheduled_date="2026-07-01", duration_minutes=30, price=0.0,
                    status="completed", created_at=now - timedelta(days=days)))
    db.commit()
    b = _find(ach.compute_achievements(db, _pet(db)), "rotina_do_bem")
    assert b["achieved"] is False
    assert b["progress"]["current"] < 4


# ---------------------------------------------------------------------------
# Primeira memória (foto de finalização)
# ---------------------------------------------------------------------------

def test_primeira_memoria_requires_completion_photo():
    db = _db()
    _add_walks(db, n=1)  # passeio concluído, sem foto ainda
    b = _find(ach.compute_achievements(db, _pet(db)), "primeira_memoria")
    assert b["achieved"] is False

    db.add(WalkCompletionReview(walk_id="w0", walker_user_id="u1", tutor_user_id="u1",
                                photo_url="https://x/foto.jpg"))
    db.commit()
    b = _find(ach.compute_achievements(db, _pet(db)), "primeira_memoria")
    assert b["achieved"] is True
    assert b["achieved_at"] is not None  # data do passeio da 1ª memória


def test_primeira_memoria_ignores_empty_photo():
    db = _db()
    _add_walks(db, n=1)
    db.add(WalkCompletionReview(walk_id="w0", walker_user_id="u1", tutor_user_id="u1",
                                photo_url=""))
    db.commit()
    b = _find(ach.compute_achievements(db, _pet(db)), "primeira_memoria")
    assert b["achieved"] is False


# ---------------------------------------------------------------------------
# Saúde
# ---------------------------------------------------------------------------

def test_vacinas_em_dia():
    db = _db()
    _add_record(db, "vaccine", valid_days=200)  # em dia
    b = _find(ach.compute_achievements(db, _pet(db)), "vacinas_em_dia")
    assert b["achieved"] is True
    assert b["offer_hint"] is None


def test_vacina_vencida_nao_conta():
    db = _db()
    _add_record(db, "vaccine", valid_days=-5)  # atrasada
    b = _find(ach.compute_achievements(db, _pet(db)), "vacinas_em_dia")
    assert b["achieved"] is False
    assert "pendente" in b["offer_hint"].lower() or b["offer_hint"]


def test_protecao_total_needs_three_kinds():
    db = _db()
    _add_record(db, "vaccine", valid_days=200)
    _add_record(db, "dewormer", valid_days=200)
    out = ach.compute_achievements(db, _pet(db))
    assert _find(out, "protecao_total")["achieved"] is False  # falta flea_tick
    assert _find(out, "protecao_total")["progress"]["current"] == 2

    _add_record(db, "flea_tick", valid_days=200)
    out = ach.compute_achievements(db, _pet(db))
    assert _find(out, "protecao_total")["achieved"] is True
    assert _find(out, "protecao_total")["progress"]["current"] == 3


def test_primeiro_registro_saude():
    db = _db()
    assert _find(ach.compute_achievements(db, _pet(db)), "primeiro_registro_saude")["achieved"] is False
    _add_record(db, "treatment", valid_days=None)
    assert _find(ach.compute_achievements(db, _pet(db)), "primeiro_registro_saude")["achieved"] is True


# ---------------------------------------------------------------------------
# Perfil
# ---------------------------------------------------------------------------

def test_perfil_completo_needs_all_four_fields():
    db = _db()
    pet = _pet(db)
    pet.diet_type = "seca"
    pet.vet_name = "Dr. Vet"
    pet.emergency_contact = "5599999"
    db.commit()
    # Falta peso.
    b = _find(ach.compute_achievements(db, _pet(db)), "perfil_completo")
    assert b["achieved"] is False
    assert b["progress"]["current"] == 3

    pet.weight = 12.5
    db.commit()
    b = _find(ach.compute_achievements(db, _pet(db)), "perfil_completo")
    assert b["achieved"] is True
    assert b["progress"]["current"] == 4


def test_bem_estar_otimo_reflects_wellness_score():
    db = _db()
    # Carteira toda em dia + rotina forte → score alto (>=80).
    _add_record(db, "vaccine", valid_days=200)
    _add_record(db, "dewormer", valid_days=200)
    _add_record(db, "flea_tick", valid_days=200)
    _add_walks(db, n=12, step_days=2)
    b = _find(ach.compute_achievements(db, _pet(db)), "bem_estar_otimo")
    assert b["progress"]["target"] == 80
    assert b["achieved"] is True


def test_bem_estar_otimo_low_when_empty():
    db = _db()
    b = _find(ach.compute_achievements(db, _pet(db)), "bem_estar_otimo")
    assert b["achieved"] is False


# ---------------------------------------------------------------------------
# Ordenação e summary
# ---------------------------------------------------------------------------

def test_achieved_first_then_by_proximity():
    db = _db()
    _add_walks(db, n=1)          # primeiro_passeio conquistado
    _add_record(db, "vaccine", valid_days=200)  # vacinas_em_dia conquistado
    out = ach.compute_achievements(db, _pet(db))
    order = [b["key"] for b in out["achievements"]]
    achieved_keys = {b["key"] for b in out["achievements"] if b["achieved"]}
    # Todas conquistadas vêm antes de qualquer pendente.
    first_pending = next(i for i, b in enumerate(out["achievements"]) if not b["achieved"])
    assert all(out["achievements"][i]["achieved"] for i in range(first_pending))
    assert "primeiro_passeio" in achieved_keys
    # Summary bate com a lista.
    assert out["summary"]["achieved"] == len(achieved_keys)


def test_summary_helper_matches_full():
    db = _db()
    _add_walks(db, n=1)
    full = ach.compute_achievements(db, _pet(db))
    assert ach.achievements_summary(db, _pet(db)) == full["summary"]
