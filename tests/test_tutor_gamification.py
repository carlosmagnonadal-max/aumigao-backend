"""Testes da gamificacao do TUTOR (GET /tutors/me/gamification).

FastAPI minimo + SQLite StaticPool + walks de exemplo. NAO importa app.main.
Valida que o backend espelha a logica do front (XP, nivel, streak, badges).
"""
from datetime import datetime, timedelta
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  — registra todas as tabelas no Base.metadata
from app.core.database import Base, get_db
from app.dependencies.auth import get_current_user
from app.models.pet import Pet
from app.models.user import User
from app.models.walk import Walk
from app.routes import tutor_gamification

TENANT_ID = "t-test"
TUTOR_ID = "tutor-test"

NOW = datetime(2026, 6, 9, 12, 0, 0)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def build(*, walks: list[dict] | None = None, pets: int = 0):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    db.add(User(id=TUTOR_ID, email="tutor@test.com", password_hash="x", role="cliente", tenant_id=TENANT_ID))
    for i in range(pets):
        db.add(Pet(id=f"pet-{i}", tutor_id=TUTOR_ID, name=f"Pet {i}"))
    for idx, w in enumerate(walks or []):
        db.add(Walk(
            id=w.get("id", f"walk-{idx}"),
            tutor_id=w.get("tutor_id", TUTOR_ID),
            pet_id=w.get("pet_id", "pet-0"),
            scheduled_date=w["scheduled_date"],
            duration_minutes=w.get("duration_minutes", 45),
            price=w.get("price", 50.0),
            status=w.get("status", "Agendado"),
            operational_status=w.get("operational_status", w.get("status", "ride_scheduled")),
        ))
    db.commit()

    test_app = FastAPI()
    test_app.include_router(tutor_gamification.router)
    test_app.dependency_overrides[get_db] = lambda: db
    test_app.dependency_overrides[get_current_user] = lambda: db.get(User, TUTOR_ID)
    return TestClient(test_app), db


def _get(client) -> dict:
    r = client.get("/tutors/me/gamification")
    assert r.status_code == 200, r.text
    return r.json()


def _get_at(client, now: datetime) -> dict:
    """Chama a rota com o relógio fixo em `now`.

    Determinístico: independe da data real do sistema. Faz patch do
    `datetime.now` no módulo do serviço para que `get_tutor_gamification`
    receba o timestamp correto sem alterar o comportamento de produção
    (onde a rota não passa `now` e o serviço usa `datetime.now` real).
    """
    import datetime as _stdlib_dt

    class _FakeDatetime(_stdlib_dt.datetime):
        """Subclasse que substitui `now()` pelo instante fixo `now`."""
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            if tz is not None:
                return now.replace(tzinfo=_stdlib_dt.timezone.utc)
            return now

    with patch("app.services.tutor_gamification_service.datetime", _FakeDatetime):
        r = client.get("/tutors/me/gamification")
    assert r.status_code == 200, r.text
    return r.json()


# ----- estado vazio: tutor sem passeios -----
def test_empty_tutor_starts_at_level_one():
    client, _ = build()
    body = _get(client)
    assert body["tutor_id"] == TUTOR_ID
    assert body["tutor_xp"] == 0
    assert body["tutor_level"] == 1
    assert body["tutor_level_title"] == "Tutor iniciante"
    assert body["total_walks_completed"] == 0
    assert body["total_pets_registered"] == 0
    assert body["care_streak_days"] == 0
    assert body["last_care_action_at"] is None
    # nenhum badge desbloqueado
    assert all(b["status"] == "locked" for b in body["badges"])
    assert {b["type"] for b in body["badges"]} == {
        "first_care", "dedicated_tutor", "care_week", "complete_family", "premium_tutor",
    }


# ----- XP: concluidos*45 + agendados*10 -----
def test_xp_formula_completed_and_scheduled():
    walks = [
        {"id": "c1", "scheduled_date": _iso(NOW - timedelta(days=3)), "status": "Finalizado", "operational_status": "ride_completed"},
        {"id": "c2", "scheduled_date": _iso(NOW - timedelta(days=2)), "status": "Finalizado", "operational_status": "ride_completed"},
        {"id": "s1", "scheduled_date": _iso(NOW + timedelta(days=1)), "status": "Agendado", "operational_status": "ride_scheduled"},
    ]
    client, _ = build(walks=walks)
    # Injeta NOW fixo: s1 (NOW+1d) deve ser futuro em relação a NOW,
    # independente da data real do sistema (evita flakiness).
    body = _get_at(client, NOW)
    # 2 concluidos * 45 + 1 agendado * 10 = 100
    assert body["total_walks_completed"] == 2
    assert body["tutor_xp"] == 100
    # 100 XP = corte do nivel 2
    assert body["tutor_level"] == 2
    assert body["tutor_level_title"] == "Tutor cuidadoso"


def test_cancelled_walks_do_not_count():
    walks = [
        {"id": "x1", "scheduled_date": _iso(NOW - timedelta(days=1)), "status": "Finalizado", "operational_status": "ride_cancelled"},
        {"id": "x2", "scheduled_date": _iso(NOW + timedelta(days=1)), "status": "Cancelado", "operational_status": "ride_cancelled"},
    ]
    client, _ = build(walks=walks)
    body = _get(client)
    assert body["total_walks_completed"] == 0
    assert body["tutor_xp"] == 0


# ----- care streak: dias consecutivos com passeio concluido -----
def test_care_streak_consecutive_days():
    walks = [
        {"id": "d0", "scheduled_date": _iso(NOW), "status": "Finalizado"},
        {"id": "d1", "scheduled_date": _iso(NOW - timedelta(days=1)), "status": "Finalizado"},
        {"id": "d2", "scheduled_date": _iso(NOW - timedelta(days=2)), "status": "Finalizado"},
        # gap: nada no dia -3
        {"id": "d4", "scheduled_date": _iso(NOW - timedelta(days=4)), "status": "Finalizado"},
    ]
    client, _ = build(walks=walks)
    # Injeta NOW fixo: o streak é calculado a partir de "hoje" (NOW),
    # portanto deve ser determinístico independente da data real.
    body = _get_at(client, NOW)
    assert body["care_streak_days"] == 3


def test_care_streak_breaks_if_no_recent_walk():
    walks = [
        {"id": "old", "scheduled_date": _iso(NOW - timedelta(days=5)), "status": "Finalizado"},
    ]
    client, _ = build(walks=walks)
    # Injeta NOW fixo: NOW-5d é mais antigo que "ontem" relativo a NOW,
    # portanto streak=0. Sem o fix, quando real_now > NOW+5d, o walk seria
    # ainda mais antigo e o resultado casual seria 0 de qualquer forma —
    # mas com NOW fixo garantimos a intenção do teste independente do tempo.
    body = _get_at(client, NOW)
    assert body["care_streak_days"] == 0


# ----- badges -----
def test_first_care_badge_unlocks_with_scheduled_walk():
    walks = [
        {"id": "s1", "scheduled_date": _iso(NOW + timedelta(days=1)), "status": "Agendado", "operational_status": "ride_scheduled"},
    ]
    client, _ = build(walks=walks)
    # Injeta NOW fixo: s1 (NOW+1d) deve ser futuro → scheduled=1 → badge desbloqueado.
    body = _get_at(client, NOW)
    badge = next(b for b in body["badges"] if b["type"] == "first_care")
    assert badge["status"] == "unlocked"
    assert badge["unlockedAt"] is not None


def test_dedicated_tutor_badge_at_three_completed():
    walks = [
        {"id": f"c{i}", "scheduled_date": _iso(NOW - timedelta(days=i)), "status": "Finalizado"}
        for i in range(3)
    ]
    client, _ = build(walks=walks)
    body = _get(client)
    badge = next(b for b in body["badges"] if b["type"] == "dedicated_tutor")
    assert badge["status"] == "unlocked"


def test_complete_family_badge_with_registered_pet():
    client, _ = build(pets=1)
    body = _get(client)
    badge = next(b for b in body["badges"] if b["type"] == "complete_family")
    assert badge["status"] == "unlocked"
    assert body["total_pets_registered"] == 1


def test_premium_tutor_badge_at_level_five():
    # 20 concluidos * 45 = 900 XP -> nivel 5
    walks = [
        {"id": f"c{i}", "scheduled_date": _iso(NOW - timedelta(days=30 + i)), "status": "Finalizado"}
        for i in range(20)
    ]
    client, _ = build(walks=walks)
    body = _get(client)
    assert body["tutor_xp"] == 900
    assert body["tutor_level"] == 5
    assert body["tutor_level_title"] == "Tutor premium"
    badge = next(b for b in body["badges"] if b["type"] == "premium_tutor")
    assert badge["status"] == "unlocked"
    assert body["next_level_xp"] is None
    assert body["xp_to_next_level"] is None
    assert body["level_progress_percentage"] == 100


def test_other_tutor_walks_are_ignored():
    walks = [
        {"id": "mine", "scheduled_date": _iso(NOW - timedelta(days=1)), "status": "Finalizado"},
        {"id": "theirs", "tutor_id": "someone-else", "scheduled_date": _iso(NOW - timedelta(days=1)), "status": "Finalizado"},
    ]
    client, db = build(walks=walks)
    db.add(User(id="someone-else", email="o@test.com", password_hash="x", role="cliente", tenant_id=TENANT_ID))
    db.commit()
    body = _get(client)
    assert body["total_walks_completed"] == 1
