"""Testes das coordenadas do destino do Pet Tour (mig 0101).

Mesmo padrao do tests/test_meeting_point.py: FastAPI minimo com o router de
walks, SQLite em memoria (StaticPool), overrides de get_db/get_current_user.

Cobre:
  - create_walk aceita destination + par de coordenadas e devolve os 3.
  - create_walk recusa par parcial (lat sem lng) com 400.
  - create_walk recusa coordenadas sem destination em texto com 400.
  - create_walk sem coordenadas (só texto, legado) segue OK.
  - Validacao Pydantic recusa lng fora de [-180, 180] com 422.
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

TENANT_ID = "t-dc"
TUTOR_ID = "tutor-dc"
PET_ID = "pet-dc"


def build():
    """Monta app minimo com o router de walks e SQLite em memoria isolado."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    db.add(Tenant(id=TENANT_ID, name="Aumigao", slug=DEFAULT_TENANT_SLUG, status="active", plan="business"))
    db.add(User(id=TUTOR_ID, email="tutor-dc@test.com", password_hash="x", role="cliente", tenant_id=TENANT_ID))
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
        "scheduled_date": "2026-08-02T09:00:00",
        "duration_minutes": 45,
        "price": 49.9,
        "pickup_method": "Buscar em casa",
    }
    base.update(extra)
    return base


def test_create_walk_with_destination_coords_succeeds():
    """destination + par lat/lng é aceito e devolvido na confirmação."""
    client, _ = build()
    res = client.post(
        "/walks",
        json=_payload(
            destination="Parque da Cidade, Salvador",
            destination_lat=-12.9818,
            destination_lng=-38.4652,
        ),
    )
    assert res.status_code in (200, 201), res.text
    body = res.json()
    assert body.get("destination") == "Parque da Cidade, Salvador"
    assert body.get("destination_lat") == pytest.approx(-12.9818)
    assert body.get("destination_lng") == pytest.approx(-38.4652)


def test_create_walk_with_partial_pair_rejected():
    """destination_lat sem destination_lng deve ser rejeitado com 400."""
    client, _ = build()
    res = client.post(
        "/walks",
        json=_payload(
            scheduled_date="2026-08-02T10:00:00",
            destination="Parque da Cidade",
            destination_lat=-12.9818,
        ),
    )
    assert res.status_code == 400, res.text
    assert "destination" in res.json().get("detail", "").lower()


def test_create_walk_coords_without_destination_rejected():
    """Coordenadas sem destination em texto devem ser rejeitadas com 400."""
    client, _ = build()
    res = client.post(
        "/walks",
        json=_payload(
            scheduled_date="2026-08-02T11:00:00",
            destination_lat=-12.9818,
            destination_lng=-38.4652,
        ),
    )
    assert res.status_code == 400, res.text
    assert "destination" in res.json().get("detail", "").lower()


def test_create_walk_destination_text_only_succeeds():
    """Pet Tour legado (só texto, sem mapa/flag OFF) segue funcionando."""
    client, _ = build()
    res = client.post(
        "/walks",
        json=_payload(
            scheduled_date="2026-08-02T12:00:00",
            destination="Trilha do Abaeté",
        ),
    )
    assert res.status_code in (200, 201), res.text
    body = res.json()
    assert body.get("destination") == "Trilha do Abaeté"
    assert body.get("destination_lat") is None
    assert body.get("destination_lng") is None


def test_destination_lng_out_of_range_rejected():
    """Validação Pydantic recusa lng fora de [-180, 180] → 422."""
    client, _ = build()
    res = client.post(
        "/walks",
        json=_payload(
            scheduled_date="2026-08-02T13:00:00",
            destination="Lugar X",
            destination_lat=-12.98,
            destination_lng=-200.0,  # inválido (< -180)
        ),
    )
    assert res.status_code == 422, res.text
