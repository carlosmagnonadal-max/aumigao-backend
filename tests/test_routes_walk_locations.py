"""Testes de ROTA (camada HTTP) para app/routes/walk_locations.py.

Segue o padrão do projeto: FastAPI mínimo com SQLite em memória (StaticPool),
dependency_overrides de get_db/get_current_user. NUNCA importa app.main.

Cobre:
- POST walker atribuído → 200 salva pings
- POST walker não-atribuído → 403
- POST status não-ativo → 409
- POST validação lat/lng fora do range → 422
- POST lote de 30 itens salva todos
- GET tutor dono vê trajeto → 200
- GET walker atribuído vê trajeto → 200
- GET outro usuário → 403
- GET ?since= polling incremental (filtra pings antigos)
- GET limit respeita valor fornecido
- has_live_tracking em serialize_operational_walk
"""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 — registra todas as tabelas no Base.metadata
from app.core.database import Base, get_db
from app.dependencies.auth import get_current_user
from app.models.pet import Pet
from app.models.tenant import Tenant
from app.models.user import User
from app.models.walk import Walk
from app.models.walk_location_ping import WalkLocationPing
from app.routes import walk_locations
from app.services.operational_matching_service import WALKER_ARRIVING, RIDE_IN_PROGRESS

TENANT_ID = "t-loc-test"
TUTOR_ID = "tutor-loc"
WALKER_ID = "walker-loc"
OTHER_WALKER_ID = "walker-other-loc"
OTHER_TUTOR_ID = "tutor-other-loc"
ADMIN_ID = "admin-loc"
WALK_ID = "walk-loc-1"
PET_ID = "pet-loc"


class _CurrentUser:
    def __init__(self, db):
        self.db = db
        self.user_id = TUTOR_ID

    def __call__(self):
        return self.db.get(User, self.user_id)


def build(operational_status: str = WALKER_ARRIVING):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    db.add(Tenant(id=TENANT_ID, name="Aumigao", slug="aumigao-loc", status="active", plan="business"))
    db.add(User(id=TUTOR_ID, email="tutor-loc@test.com", password_hash="x", role="cliente", tenant_id=TENANT_ID))
    db.add(User(id=WALKER_ID, email="walker-loc@test.com", password_hash="x", role="walker", tenant_id=TENANT_ID))
    db.add(User(id=OTHER_WALKER_ID, email="walker-other-loc@test.com", password_hash="x", role="walker", tenant_id=TENANT_ID))
    db.add(User(id=OTHER_TUTOR_ID, email="tutor-other-loc@test.com", password_hash="x", role="cliente", tenant_id=TENANT_ID))
    db.add(User(id=ADMIN_ID, email="admin-loc@test.com", password_hash="x", role="admin", tenant_id=TENANT_ID))
    db.add(Pet(id=PET_ID, tutor_id=TUTOR_ID, name="Bolt", tenant_id=TENANT_ID))
    db.add(
        Walk(
            id=WALK_ID,
            tutor_id=TUTOR_ID,
            walker_id=WALKER_ID,
            tenant_id=TENANT_ID,
            pet_id=PET_ID,
            scheduled_date="2026-06-12T14:00:00",
            duration_minutes=45,
            price=50.0,
            status="Indo buscar o pet",
            operational_status=operational_status,
            walker_selection_mode="auto",
        )
    )
    db.commit()

    current = _CurrentUser(db)
    test_app = FastAPI()
    test_app.include_router(walk_locations.router)
    test_app.dependency_overrides[get_db] = lambda: db
    test_app.dependency_overrides[get_current_user] = current
    return TestClient(test_app, raise_server_exceptions=True), db, current


def _ping_payload(n: int = 1, lat: float = -23.5, lng: float = -46.6):
    return {
        "pings": [
            {
                "latitude": lat,
                "longitude": lng,
                "accuracy": 10.0,
                "recorded_at": f"2026-06-12T14:{i:02d}:00Z",
            }
            for i in range(n)
        ]
    }


# ---------------------------------------------------------------------------
# POST: walker atribuído → salva pings
# ---------------------------------------------------------------------------

def test_post_pings_walker_atribuido_salva():
    client, db, current = build()
    current.user_id = WALKER_ID
    r = client.post(f"/walks/{WALK_ID}/locations", json=_ping_payload(3))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["saved"] == 3
    assert "limit_reached" not in body or body.get("limit_reached") is False

    # Verifica que foram realmente persistidos no banco.
    saved = db.query(WalkLocationPing).filter(WalkLocationPing.walk_id == WALK_ID).count()
    assert saved == 3


# ---------------------------------------------------------------------------
# POST: walker não-atribuído → 403
# ---------------------------------------------------------------------------

def test_post_pings_walker_nao_atribuido_403():
    client, db, current = build()
    current.user_id = OTHER_WALKER_ID
    r = client.post(f"/walks/{WALK_ID}/locations", json=_ping_payload(1))
    assert r.status_code == 403, r.text


# ---------------------------------------------------------------------------
# POST: status não-ativo → 409
# ---------------------------------------------------------------------------

def test_post_pings_status_nao_ativo_409():
    client, db, current = build(operational_status="ride_scheduled")
    current.user_id = WALKER_ID
    r = client.post(f"/walks/{WALK_ID}/locations", json=_ping_payload(1))
    assert r.status_code == 409, r.text
    assert "ativo" in r.json()["detail"].lower() or "status" in r.json()["detail"].lower()


def test_post_pings_status_concluido_409():
    client, db, current = build(operational_status="ride_completed")
    current.user_id = WALKER_ID
    r = client.post(f"/walks/{WALK_ID}/locations", json=_ping_payload(1))
    assert r.status_code == 409, r.text


# ---------------------------------------------------------------------------
# POST: walk em RIDE_IN_PROGRESS também é ativo
# ---------------------------------------------------------------------------

def test_post_pings_ride_in_progress_salva():
    client, db, current = build(operational_status=RIDE_IN_PROGRESS)
    current.user_id = WALKER_ID
    r = client.post(f"/walks/{WALK_ID}/locations", json=_ping_payload(2))
    assert r.status_code == 200, r.text
    assert r.json()["saved"] == 2


# ---------------------------------------------------------------------------
# POST: validação lat/lng fora do range → 422
# ---------------------------------------------------------------------------

def test_post_pings_latitude_invalida_422():
    client, db, current = build()
    current.user_id = WALKER_ID
    payload = _ping_payload(1)
    payload["pings"][0]["latitude"] = 95.0  # fora de [-90, 90]
    r = client.post(f"/walks/{WALK_ID}/locations", json=payload)
    assert r.status_code == 422, r.text


def test_post_pings_longitude_invalida_422():
    client, db, current = build()
    current.user_id = WALKER_ID
    payload = _ping_payload(1)
    payload["pings"][0]["longitude"] = 200.0  # fora de [-180, 180]
    r = client.post(f"/walks/{WALK_ID}/locations", json=payload)
    assert r.status_code == 422, r.text


# ---------------------------------------------------------------------------
# POST: lote de 30 itens → todos salvos
# ---------------------------------------------------------------------------

def test_post_pings_lote_30():
    client, db, current = build()
    current.user_id = WALKER_ID
    r = client.post(f"/walks/{WALK_ID}/locations", json=_ping_payload(30))
    assert r.status_code == 200, r.text
    assert r.json()["saved"] == 30


# ---------------------------------------------------------------------------
# POST: lote de 31 itens → 422 (max_length=30)
# ---------------------------------------------------------------------------

def test_post_pings_lote_31_422():
    client, db, current = build()
    current.user_id = WALKER_ID
    r = client.post(f"/walks/{WALK_ID}/locations", json=_ping_payload(31))
    assert r.status_code == 422, r.text


# ---------------------------------------------------------------------------
# GET: tutor dono vê trajeto
# ---------------------------------------------------------------------------

def test_get_locations_tutor_ve_trajeto():
    client, db, current = build()
    # Insere ping diretamente no banco.
    db.add(WalkLocationPing(
        id=str(uuid4()),
        walk_id=WALK_ID,
        walker_id=WALKER_ID,
        latitude=-23.5,
        longitude=-46.6,
        accuracy=8.0,
        recorded_at=datetime(2026, 6, 12, 14, 0, 0),
        created_at=datetime.utcnow(),
    ))
    db.commit()

    current.user_id = TUTOR_ID
    r = client.get(f"/walks/{WALK_ID}/locations")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["walk_id"] == WALK_ID
    assert body["count"] == 1
    assert len(body["pings"]) == 1
    assert body["pings"][0]["latitude"] == -23.5


# ---------------------------------------------------------------------------
# GET: walker atribuído vê trajeto
# ---------------------------------------------------------------------------

def test_get_locations_walker_atribuido_ve_trajeto():
    client, db, current = build()
    db.add(WalkLocationPing(
        id=str(uuid4()),
        walk_id=WALK_ID,
        walker_id=WALKER_ID,
        latitude=-23.5,
        longitude=-46.6,
        accuracy=None,
        recorded_at=datetime(2026, 6, 12, 14, 0, 0),
        created_at=datetime.utcnow(),
    ))
    db.commit()

    current.user_id = WALKER_ID
    r = client.get(f"/walks/{WALK_ID}/locations")
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 1


# ---------------------------------------------------------------------------
# GET: outro tutor (não dono do walk) → 403
# ---------------------------------------------------------------------------

def test_get_locations_outro_tutor_403():
    client, db, current = build()
    current.user_id = OTHER_TUTOR_ID
    r = client.get(f"/walks/{WALK_ID}/locations")
    assert r.status_code == 403, r.text


# ---------------------------------------------------------------------------
# GET: walker não-atribuído → 403
# ---------------------------------------------------------------------------

def test_get_locations_walker_nao_atribuido_403():
    client, db, current = build()
    current.user_id = OTHER_WALKER_ID
    r = client.get(f"/walks/{WALK_ID}/locations")
    assert r.status_code == 403, r.text


# ---------------------------------------------------------------------------
# GET: ?since= filtra polling incremental
# ---------------------------------------------------------------------------

def test_get_locations_since_filtra_antigos():
    client, db, current = build()
    # Ping antigo (deve ser excluído pelo since)
    db.add(WalkLocationPing(
        id=str(uuid4()),
        walk_id=WALK_ID,
        walker_id=WALKER_ID,
        latitude=-23.5,
        longitude=-46.6,
        accuracy=None,
        recorded_at=datetime(2026, 6, 12, 13, 0, 0),
        created_at=datetime.utcnow(),
    ))
    # Ping novo (deve aparecer)
    db.add(WalkLocationPing(
        id=str(uuid4()),
        walk_id=WALK_ID,
        walker_id=WALKER_ID,
        latitude=-23.6,
        longitude=-46.7,
        accuracy=5.0,
        recorded_at=datetime(2026, 6, 12, 14, 30, 0),
        created_at=datetime.utcnow(),
    ))
    db.commit()

    current.user_id = TUTOR_ID
    # since = 14:00:00 → deve retornar apenas o ping das 14:30
    r = client.get(f"/walks/{WALK_ID}/locations?since=2026-06-12T14:00:00")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 1
    assert body["pings"][0]["latitude"] == -23.6


# ---------------------------------------------------------------------------
# GET: ?limit= respeita limite
# ---------------------------------------------------------------------------

def test_get_locations_limit_respeitado():
    client, db, current = build()
    for i in range(10):
        db.add(WalkLocationPing(
            id=str(uuid4()),
            walk_id=WALK_ID,
            walker_id=WALKER_ID,
            latitude=-23.5,
            longitude=-46.6,
            accuracy=None,
            recorded_at=datetime(2026, 6, 12, 14, i, 0),
            created_at=datetime.utcnow(),
        ))
    db.commit()

    current.user_id = TUTOR_ID
    r = client.get(f"/walks/{WALK_ID}/locations?limit=3")
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 3


# ---------------------------------------------------------------------------
# GET: resposta inclui walk_status
# ---------------------------------------------------------------------------

def test_get_locations_inclui_walk_status():
    client, db, current = build()
    current.user_id = TUTOR_ID
    r = client.get(f"/walks/{WALK_ID}/locations")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "walk_status" in body
    assert body["walk_status"] == WALKER_ARRIVING


# ---------------------------------------------------------------------------
# has_live_tracking em serialize_operational_walk
# ---------------------------------------------------------------------------

def test_has_live_tracking_false_sem_pings():
    from app.services.operational_matching_service import serialize_operational_walk
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    db.add(Tenant(id="t-ht", name="T", slug="t-ht-slug", status="active", plan="business"))
    db.add(User(id="tutor-ht", email="ht@test.com", password_hash="x", role="cliente", tenant_id="t-ht"))
    db.add(Pet(id="pet-ht", tutor_id="tutor-ht", name="Rex", tenant_id="t-ht"))
    walk = Walk(
        id="walk-ht",
        tutor_id="tutor-ht",
        pet_id="pet-ht",
        tenant_id="t-ht",
        scheduled_date="2026-06-12T14:00:00",
        duration_minutes=30,
        price=40.0,
        status="Indo buscar o pet",
        operational_status=WALKER_ARRIVING,
        walker_selection_mode="auto",
    )
    db.add(walk)
    db.commit()

    result = serialize_operational_walk(walk, db)
    assert result["has_live_tracking"] is False


def test_has_live_tracking_true_com_ping_recente():
    from app.services.operational_matching_service import serialize_operational_walk
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    db.add(Tenant(id="t-ht2", name="T2", slug="t-ht2-slug", status="active", plan="business"))
    db.add(User(id="tutor-ht2", email="ht2@test.com", password_hash="x", role="cliente", tenant_id="t-ht2"))
    db.add(User(id="walker-ht2", email="w-ht2@test.com", password_hash="x", role="walker", tenant_id="t-ht2"))
    db.add(Pet(id="pet-ht2", tutor_id="tutor-ht2", name="Rex2", tenant_id="t-ht2"))
    walk = Walk(
        id="walk-ht2",
        tutor_id="tutor-ht2",
        walker_id="walker-ht2",
        pet_id="pet-ht2",
        tenant_id="t-ht2",
        scheduled_date="2026-06-12T14:00:00",
        duration_minutes=30,
        price=40.0,
        status="Passeando agora",
        operational_status=RIDE_IN_PROGRESS,
        walker_selection_mode="auto",
    )
    db.add(walk)
    # Ping recente (agora)
    db.add(WalkLocationPing(
        id=str(uuid4()),
        walk_id="walk-ht2",
        walker_id="walker-ht2",
        latitude=-23.5,
        longitude=-46.6,
        accuracy=None,
        recorded_at=datetime.utcnow(),
        created_at=datetime.utcnow(),
    ))
    db.commit()

    result = serialize_operational_walk(walk, db)
    assert result["has_live_tracking"] is True
