"""Testes da rotina/evolucao do pet (compute-based, GET /pets/{id}/routine).

FastAPI minimo + SQLite StaticPool + overrides de get_db / get_current_user.
NAO importa app.main (que conecta no banco real). Cria pet + walks e valida o
espelho da logica do front (status por horas, xp = passeios*35 + semana*10,
niveis, badges, ownership).
"""
from datetime import datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 — registra todas as tabelas no Base.metadata
from app.core.database import Base, get_db
from app.dependencies.auth import get_current_user
from app.models.pet import Pet
from app.models.user import User
from app.models.walk import Walk
from app.routes import pet_routine

TUTOR_ID = "tutor-1"
OTHER_ID = "tutor-2"
PET_ID = "pet-1"


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def build(*, pet_tutor: str = TUTOR_ID, current: str = TUTOR_ID, with_pet: bool = True):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    db.add(User(id=TUTOR_ID, email="t1@test.com", password_hash="x", role="cliente"))
    db.add(User(id=OTHER_ID, email="t2@test.com", password_hash="x", role="cliente"))
    if with_pet:
        db.add(Pet(id=PET_ID, tutor_id=pet_tutor, name="Thor", breed="Golden", size="Medio", age=3))
    db.commit()

    app_ = FastAPI()
    app_.include_router(pet_routine.router)
    app_.dependency_overrides[get_db] = lambda: db
    app_.dependency_overrides[get_current_user] = lambda: db.get(User, current)
    return TestClient(app_), db


def _add_walk(db, *, walk_id: str, when: datetime, status: str = "Finalizado", pet_id: str = PET_ID):
    db.add(
        Walk(
            id=walk_id,
            tutor_id=TUTOR_ID,
            pet_id=pet_id,
            scheduled_date=_iso(when),
            duration_minutes=30,
            price=50.0,
            status=status,
        )
    )
    db.commit()


# --------------------------------------------------------------------------- #
def test_pet_not_found():
    client, _ = build(with_pet=False)
    assert client.get(f"/pets/{PET_ID}/routine").status_code == 404


def test_ownership_enforced():
    client, _ = build(current=OTHER_ID)
    assert client.get(f"/pets/{PET_ID}/routine").status_code == 403


def test_no_walks_status_undefined():
    client, _ = build()
    body = client.get(f"/pets/{PET_ID}/routine").json()
    assert body["pet_id"] == PET_ID
    assert body["tutor_id"] == TUTOR_ID
    assert body["current_status"] == "undefined"
    assert body["last_walk_at"] is None
    assert body["weekly_walk_count"] == 0
    assert body["xp"] == 0
    assert body["level"] == 1
    assert body["routine_progress_percentage"] == 0
    # nenhum badge desbloqueado
    assert all(b["status"] == "locked" for b in body["badges"])
    assert body["next_badge"]["type"] == "first_walk"


def test_only_completed_walks_count():
    client, db = build()
    now = datetime.utcnow()
    _add_walk(db, walk_id="w-agendado", when=now - timedelta(hours=2), status="Agendado")
    body = client.get(f"/pets/{PET_ID}/routine").json()
    # passeio nao concluido nao conta
    assert body["current_status"] == "undefined"
    assert body["xp"] == 0


def test_status_post_walk_satisfied_recent():
    client, db = build()
    now = datetime.utcnow()
    _add_walk(db, walk_id="w1", when=now - timedelta(hours=2))
    body = client.get(f"/pets/{PET_ID}/routine").json()
    assert body["current_status"] == "post_walk_satisfied"
    # 1 passeio concluido nesta semana: xp = 1*35 + 1*10 = 45
    assert body["xp"] == 45
    first = next(b for b in body["badges"] if b["type"] == "first_walk")
    assert first["status"] == "unlocked"
    assert first["unlockedAt"] is not None


def test_status_very_active_old_walk():
    client, db = build()
    now = datetime.utcnow()
    # passeio ha 3 dias (>48h) e fora da semana atual nao garante; usamos 3 dias
    _add_walk(db, walk_id="w1", when=now - timedelta(hours=72))
    body = client.get(f"/pets/{PET_ID}/routine").json()
    assert body["current_status"] == "very_active"


def test_active_week_badge_three_walks_this_week():
    client, db = build()
    # Passeios espalhados na semana ISO atual (segunda-feira ate hoje).
    now = datetime.utcnow()
    week_start = datetime(now.year, now.month, now.day) - timedelta(days=now.weekday())
    for i in range(3):
        _add_walk(db, walk_id=f"w{i}", when=week_start + timedelta(hours=1 + i))
    body = client.get(f"/pets/{PET_ID}/routine").json()
    assert body["weekly_walk_count"] == 3
    assert body["routine_progress_percentage"] == 100
    # xp = 3*35 + 3*10 = 135 -> level 2 (>=100)
    assert body["xp"] == 135
    assert body["level"] == 2
    active = next(b for b in body["badges"] if b["type"] == "active_week")
    assert active["status"] == "unlocked"


def test_energy_controlled_badge():
    client, db = build()
    now = datetime.utcnow()
    # penultimo passeio muito antes do ultimo (previous_status very_active),
    # ultimo passeio recente (current post_walk_satisfied).
    _add_walk(db, walk_id="w-old", when=now - timedelta(days=10))
    _add_walk(db, walk_id="w-new", when=now - timedelta(hours=1))
    body = client.get(f"/pets/{PET_ID}/routine").json()
    assert body["current_status"] == "post_walk_satisfied"
    energy = next(b for b in body["badges"] if b["type"] == "energy_controlled")
    assert energy["status"] == "unlocked"
