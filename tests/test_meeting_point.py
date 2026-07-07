"""Testes do meeting_point dedicado (mig 0100).

Padrao do projeto (ver tests/test_routes_walks.py): monta um FastAPI MINIMO
com apenas o router de walks, SQLite em memoria (StaticPool), overrides de
get_db / get_current_user. NAO importa app.main.

Cobre:
  - create_walk aceita trio coerente (point + lat + lng).
  - create_walk recusa trio parcial (point sem lat, ou lat sem lng) com 400.
  - create_walk aceita trio null (pickup_method=Buscar em casa).
  - Validacao Pydantic recusa lat fora de [-90, 90] com 422.
"""
import pytest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  — registra todas as tabelas no Base.metadata
from app.core.database import Base, get_db
from app.dependencies.auth import get_current_user
from app.models.pet import Pet
from app.models.tenant import Tenant
from app.models.user import User
from app.routes import walks
from app.services.tenant_seed_service import DEFAULT_TENANT_SLUG

TENANT_ID = "t-mp"
TUTOR_ID = "tutor-mp"
WALKER_ID = "walker-mp"
PET_ID = "pet-mp"


def build():
    """Monta app minimo com o router de walks e SQLite em memoria isolado."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    db.add(Tenant(id=TENANT_ID, name="Aumigao", slug=DEFAULT_TENANT_SLUG, status="active", plan="business"))
    db.add(User(id=TUTOR_ID, email="tutor-mp@test.com", password_hash="x", role="cliente", tenant_id=TENANT_ID))
    db.add(User(id=WALKER_ID, email="walker-mp@test.com", password_hash="x", role="walker", tenant_id=TENANT_ID, is_active=True))
    db.add(Pet(id=PET_ID, tutor_id=TUTOR_ID, name="Thor", tenant_id=TENANT_ID))
    db.commit()

    test_app = FastAPI()
    test_app.include_router(walks.router)
    test_app.dependency_overrides[get_db] = lambda: db
    test_app.dependency_overrides[get_current_user] = lambda: db.get(User, TUTOR_ID)
    return TestClient(test_app), db


def _payload(**extra):
    base = {
        "pet_id": PET_ID,
        "scheduled_date": "2026-08-01T09:00:00",
        "duration_minutes": 45,
        "price": 49.9,
        "pickup_method": "Levar até ponto de encontro",
    }
    base.update(extra)
    return base


def test_create_walk_with_meeting_point_succeeds():
    """Trio coerente (point+lat+lng) é aceito e persistido."""
    client, _ = build()
    res = client.post(
        "/walks",
        json=_payload(
            meeting_point="Portão principal do Parque da Cidade",
            meeting_lat=-12.9714,
            meeting_lng=-38.5014,
        ),
    )
    assert res.status_code in (200, 201), res.text
    body = res.json()
    assert body.get("meeting_point") == "Portão principal do Parque da Cidade"
    assert body.get("meeting_lat") == pytest.approx(-12.9714)
    assert body.get("meeting_lng") == pytest.approx(-38.5014)


def test_create_walk_with_partial_trio_rejected():
    """meeting_point sem lat/lng deve ser rejeitado com 400."""
    client, _ = build()
    res = client.post(
        "/walks",
        json=_payload(
            scheduled_date="2026-08-01T10:00:00",
            duration_minutes=30,
            price=36.9,
            meeting_point="Só texto, sem coordenadas",
        ),
    )
    assert res.status_code == 400, res.text
    assert "meeting_point" in res.json().get("detail", "").lower()


def test_create_walk_without_meeting_point_succeeds():
    """pickup_method=Buscar em casa (default) → trio null é OK."""
    client, _ = build()
    res = client.post(
        "/walks",
        json=_payload(
            scheduled_date="2026-08-01T11:00:00",
            pickup_method="Buscar em casa",
        ),
    )
    assert res.status_code in (200, 201), res.text
    body = res.json()
    # Trio vem null quando não preenchido.
    assert body.get("meeting_point") in (None, "")
    assert body.get("meeting_lat") is None
    assert body.get("meeting_lng") is None


def test_meeting_point_lat_out_of_range_rejected():
    """Validação Pydantic recusa lat fora de [-90, 90] → 422."""
    client, _ = build()
    res = client.post(
        "/walks",
        json=_payload(
            scheduled_date="2026-08-01T12:00:00",
            meeting_point="Lugar X",
            meeting_lat=95.0,  # inválido (> 90)
            meeting_lng=-38.5,
        ),
    )
    # Pydantic validation error → 422 (não 400 do nosso handler).
    assert res.status_code == 422, res.text
