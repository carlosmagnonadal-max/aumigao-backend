"""Testes do desconto "levar até o ponto de encontro" (mig 0103).

Decisão de preço 07/07/2026: a âncora embute o passeador buscando em casa;
quando o TUTOR leva o pet até o ponto de encontro, o preço cai o flat
configurado pelo tenant (meeting_point_discount, default 0).

Padrao do projeto (ver tests/test_meeting_point.py): app FastAPI MINIMO com o
router de walks, SQLite em memoria (StaticPool), overrides de get_db /
get_current_user. NAO importa app.main.

Cobre:
  - Desconto aplicado quando pickup_method = levar ao ponto de encontro.
  - Preço cheio quando pickup_method = Buscar em casa (padrão).
  - Default 0 (tenant sem config explícita) → preço = âncora nova (54,90).
  - Desconto maior que o preço não deixa o preço negativo (floor 0).
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
from app.models.individual_walk_pricing import TenantIndividualWalkPricing
from app.models.pet import Pet
from app.models.tenant import Tenant
from app.models.user import User
from app.routes import walks
from app.services.tenant_seed_service import DEFAULT_TENANT_SLUG

TENANT_ID = "t-mpd"
TUTOR_ID = "tutor-mpd"
PET_ID = "pet-mpd"


def build(pricing: TenantIndividualWalkPricing | None = None):
    """App minimo com o router de walks; pricing opcional pré-semeado."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    db.add(Tenant(id=TENANT_ID, name="Aumigao", slug=DEFAULT_TENANT_SLUG, status="active", plan="business"))
    db.add(User(id=TUTOR_ID, email="tutor-mpd@test.com", password_hash="x", role="cliente", tenant_id=TENANT_ID))
    db.add(Pet(id=PET_ID, tutor_id=TUTOR_ID, name="Thor", tenant_id=TENANT_ID))
    if pricing is not None:
        db.add(pricing)
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
        "price": 1.0,  # ignorado: preço é server-authoritative
        "pickup_method": "Levar até ponto de encontro",
    }
    base.update(extra)
    return base


def _pricing(discount: float) -> TenantIndividualWalkPricing:
    return TenantIndividualWalkPricing(
        tenant_id=TENANT_ID,
        price_30=40.90,
        price_45=54.90,
        price_60=69.90,
        meeting_point_discount=discount,
    )


def test_discount_applied_on_meeting_point_pickup():
    """45min com desconto de 5,00 e tutor levando ao ponto → 49,90."""
    client, _ = build(_pricing(5.0))
    res = client.post("/walks", json=_payload())
    assert res.status_code in (200, 201), res.text
    assert res.json()["price"] == pytest.approx(49.90)


def test_full_price_on_home_pickup():
    """Buscar em casa (padrão do produto) → âncora cheia, sem desconto."""
    client, _ = build(_pricing(5.0))
    res = client.post("/walks", json=_payload(pickup_method="Buscar em casa"))
    assert res.status_code in (200, 201), res.text
    assert res.json()["price"] == pytest.approx(54.90)


def test_discount_defaults_to_zero_without_config():
    """Tenant sem config explícita: get_or_create usa defaults do modelo —
    âncora nova (54,90 no 45min) e desconto 0 mesmo levando ao ponto."""
    client, _ = build(pricing=None)
    res = client.post("/walks", json=_payload())
    assert res.status_code in (200, 201), res.text
    assert res.json()["price"] == pytest.approx(54.90)


def test_discount_applies_per_duration():
    """Desconto flat vale para todas as durações (30 → 35,90)."""
    client, _ = build(_pricing(5.0))
    res = client.post(
        "/walks",
        json=_payload(scheduled_date="2026-08-01T10:00:00", duration_minutes=30),
    )
    assert res.status_code in (200, 201), res.text
    assert res.json()["price"] == pytest.approx(35.90)


def test_discount_never_negative():
    """Desconto maior que o preço → floor em 0 (nunca preço negativo)."""
    client, _ = build(_pricing(999.0))
    res = client.post("/walks", json=_payload())
    assert res.status_code in (200, 201), res.text
    assert res.json()["price"] == pytest.approx(0.0)
