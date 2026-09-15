"""Correção de contrato GET /walks/{id} (2026-09-15).

PROBLEMA 1: o app do tutor lê `walk.emergency_triggered_at` (não
`operational_events`, nunca exposto ao tutor por privacidade — ver
app/schemas/walk.py) para renderizar a faixa "Emergência acionada às HH:MM".
Fonte: `walk_emergency_calls` (S1), acionamento MAIS RECENTE.

PROBLEMA 2: app/walker/detalhes-passeio.tsx usa este mesmo GET quando o
passeio ainda não está na agenda local do passeador — precisa dos mesmos
`safety_alerts`/`establishment_support_phone` que a listagem do passeador
(/walker/walks/*) já anexava, mas SOMENTE para o passeador designado.

Padrão do projeto (ver tests/test_routes_walks.py): FastAPI mínimo com o
router de walks, SQLite em memória (StaticPool), overrides de get_db/
get_current_user. Não importa app.main.
"""
from datetime import datetime, timedelta
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 - registra todas as tabelas no Base.metadata
from app.core.database import Base, get_db
from app.dependencies.auth import get_current_user
from app.models.pet import Pet
from app.models.tenant import Tenant, TenantSettings
from app.models.user import User
from app.models.walk import Walk
from app.models.walk_emergency_call import WalkEmergencyCall
from app.routes import walks
from app.services.tenant_seed_service import DEFAULT_TENANT_SLUG

TENANT_ID = "t-contract-fix"
TUTOR_ID = "tutor-contract-fix"
WALKER_ID = "walker-contract-fix"
OTHER_WALKER_ID = "walker-contract-fix-2"
ADMIN_ID = "admin-contract-fix"
PET_ID = "pet-contract-fix"
TUTOR_PHONE_FRAGMENT = "988887777"


def build():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    db.add(Tenant(id=TENANT_ID, name="Aumigao", slug=DEFAULT_TENANT_SLUG, status="active", plan="business"))
    db.add(TenantSettings(id="ts-contract-fix", tenant_id=TENANT_ID, support_phone="(71) 3599-9983"))
    db.add(User(id=TUTOR_ID, email="tutor-cf@test.com", password_hash="x", role="cliente", tenant_id=TENANT_ID))
    db.add(User(id=WALKER_ID, email="walker-cf@test.com", password_hash="x", role="walker", tenant_id=TENANT_ID, is_active=True))
    db.add(User(id=OTHER_WALKER_ID, email="walker-cf-2@test.com", password_hash="x", role="walker", tenant_id=TENANT_ID, is_active=True))
    db.add(User(id=ADMIN_ID, email="admin-cf@test.com", password_hash="x", role="admin", tenant_id=TENANT_ID))
    db.add(Pet(id=PET_ID, tutor_id=TUTOR_ID, name="Rex", tenant_id=TENANT_ID))
    db.commit()

    test_app = FastAPI()
    test_app.include_router(walks.router)
    test_app.dependency_overrides[get_db] = lambda: db
    test_app.dependency_overrides[get_current_user] = lambda: db.get(User, TUTOR_ID)
    return TestClient(test_app), db


def _as(client, db, user_id: str):
    client.app.dependency_overrides[get_current_user] = lambda: db.get(User, user_id)
    return client


def _add_walk(db, *, walker_id: str | None = WALKER_ID, assigned_walker_id: str | None = None,
              operational_status: str = "ride_in_progress") -> Walk:
    walk = Walk(
        id=str(uuid4()),
        tutor_id=TUTOR_ID,
        tenant_id=TENANT_ID,
        walker_id=walker_id,
        assigned_walker_id=assigned_walker_id,
        pet_id=PET_ID,
        scheduled_date="2026-09-15T10:00:00",
        duration_minutes=30,
        price=50.0,
        status="Passeando agora",
        operational_status=operational_status,
    )
    db.add(walk)
    db.commit()
    return walk


def _add_emergency_call(db, *, walk_id: str, created_at: datetime, reason: str | None = None) -> WalkEmergencyCall:
    call = WalkEmergencyCall(
        id=str(uuid4()), tenant_id=TENANT_ID, walk_id=walk_id, walker_user_id=WALKER_ID,
        tutor_user_id=TUTOR_ID, reason=reason, mode="direct", provider="none",
        status="initiated", created_at=created_at,
    )
    db.add(call)
    db.commit()
    return call


# ── PROBLEMA 1: emergency_triggered_at ───────────────────────────────────────

def test_emergency_triggered_at_none_without_emergency():
    client, db = build()
    walk = _add_walk(db)
    body = client.get(f"/walks/{walk.id}").json()
    assert body["emergency_triggered_at"] is None


def test_emergency_triggered_at_present_after_trigger():
    client, db = build()
    walk = _add_walk(db)
    when = datetime(2026, 9, 15, 13, 30, 0)
    _add_emergency_call(db, walk_id=walk.id, created_at=when)
    body = client.get(f"/walks/{walk.id}").json()
    assert body["emergency_triggered_at"] == "2026-09-15T13:30:00Z"


def test_emergency_triggered_at_uses_most_recent_call():
    client, db = build()
    walk = _add_walk(db)
    _add_emergency_call(db, walk_id=walk.id, created_at=datetime(2026, 9, 15, 13, 0, 0), reason="antigo")
    _add_emergency_call(db, walk_id=walk.id, created_at=datetime(2026, 9, 15, 13, 12, 0), reason="mais_recente")
    body = client.get(f"/walks/{walk.id}").json()
    assert body["emergency_triggered_at"] == "2026-09-15T13:12:00Z"


def test_emergency_triggered_at_visible_to_designated_walker_and_admin():
    client, db = build()
    walk = _add_walk(db)
    _add_emergency_call(db, walk_id=walk.id, created_at=datetime(2026, 9, 15, 13, 30, 0))

    body_walker = _as(client, db, WALKER_ID).get(f"/walks/{walk.id}").json()
    assert body_walker["emergency_triggered_at"] == "2026-09-15T13:30:00Z"

    body_admin = _as(client, db, ADMIN_ID).get(f"/walks/{walk.id}").json()
    assert body_admin["emergency_triggered_at"] == "2026-09-15T13:30:00Z"


def test_emergency_triggered_at_never_leaks_reason_or_phone():
    client, db = build()
    walk = _add_walk(db)
    _add_emergency_call(db, walk_id=walk.id, created_at=datetime(2026, 9, 15, 13, 30, 0), reason="fuga_do_pet")
    resp = client.get(f"/walks/{walk.id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["emergency_triggered_at"] == "2026-09-15T13:30:00Z"
    # NUNCA operational_events (motivo/notas do acionamento) nem o motivo cru.
    assert "operational_events" not in body
    assert "fuga_do_pet" not in resp.text
    assert TUTOR_PHONE_FRAGMENT not in resp.text


# ── PROBLEMA 2: safety_alerts / establishment_support_phone ─────────────────

def test_designated_walker_sees_safety_alerts_and_support_phone():
    client, db = build()
    walk = _add_walk(db, walker_id=WALKER_ID)
    body = _as(client, db, WALKER_ID).get(f"/walks/{walk.id}").json()
    assert isinstance(body["safety_alerts"], list)
    assert body["establishment_support_phone"] == "+557135999983"


def test_designated_via_assigned_walker_id_also_sees_fields():
    client, db = build()
    walk = _add_walk(db, walker_id=None, assigned_walker_id=WALKER_ID)
    body = _as(client, db, WALKER_ID).get(f"/walks/{walk.id}").json()
    assert isinstance(body["safety_alerts"], list)
    assert body["establishment_support_phone"] == "+557135999983"


def test_tutor_does_not_see_safety_alerts_or_support_phone():
    client, db = build()
    walk = _add_walk(db, walker_id=WALKER_ID)
    body = client.get(f"/walks/{walk.id}").json()  # default override = TUTOR_ID
    assert body["safety_alerts"] is None
    assert body["establishment_support_phone"] is None


def test_other_walker_not_designated_does_not_see_fields():
    client, db = build()
    walk = _add_walk(db, walker_id=WALKER_ID)
    # Outro passeador só chega aqui se _get_walk_for_user permitir; como ele não
    # é dono/atribuído/admin, a rota já bloqueia com 403 antes de qualquer coisa.
    resp = _as(client, db, OTHER_WALKER_ID).get(f"/walks/{walk.id}")
    assert resp.status_code == 403


def test_admin_does_not_see_safety_alerts_or_support_phone():
    # admin vê o passeio (permissão), mas não é o passeador designado — os
    # campos ficam None, igual ao tutor.
    client, db = build()
    walk = _add_walk(db, walker_id=WALKER_ID)
    body = _as(client, db, ADMIN_ID).get(f"/walks/{walk.id}").json()
    assert body["safety_alerts"] is None
    assert body["establishment_support_phone"] is None


def test_safety_alerts_empty_list_for_completed_walk_designated_walker():
    # S3: passeios encerrados não consultam regra local (evita histórico) —
    # mesma otimização de app/routes/walker.py::_SAFETY_ALERT_SKIP_STATUSES.
    client, db = build()
    walk = _add_walk(db, walker_id=WALKER_ID, operational_status="ride_completed")
    body = _as(client, db, WALKER_ID).get(f"/walks/{walk.id}").json()
    assert body["safety_alerts"] == []
