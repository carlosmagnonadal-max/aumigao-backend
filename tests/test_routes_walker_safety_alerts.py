"""S3 — safety_alerts nos payloads do passeador (padrão test_multitenant_walker_phase1_step3).

TAREFA EXTRA: establishment_support_phone (TenantSettings.support_phone com
fallback Tenant.contact_phone) nos mesmos payloads. Nunca é telefone do tutor.
"""
from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.core.database import Base, get_db, get_walker_self_db
from app.dependencies.auth import get_current_user
from app.models.local_rule import LocalRule
from app.models.pet import Pet
from app.models.tenant import Tenant, TenantSettings
from app.models.tenant_walker_access import TenantWalkerAccess
from app.models.tutor_profile import TutorProfile
from app.models.user import User
from app.models.walk import Walk, WalkMatchingAttempt
from app.models.walker_profile import WalkerProfile

TENANT_ID = "t-s3-walker"
WALKER_ID = "walker-s3"
TUTOR_ID = "tutor-s3"
PET_ID = "pet-s3"
EXPECTED = (
    "Salvador: cão acima de 24 kg — guia e focinheira obrigatórias em local público "
    "ou área de uso coletivo (Lei 9.108/2016)."
)
_MIGRATION_PATH = (
    Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0111_local_rules_pet_reactive.py"
)


def _seed_rows():
    spec = importlib.util.spec_from_file_location("mig_0111_walker_seed", _MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.SEED_ROWS


def build(*, city="Salvador", state="BA", weight=32.0, support_phone=None, contact_phone=None):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(Tenant(id=TENANT_ID, name="Aumigao", slug="aumigao-s3", status="active", plan="business",
                  contact_phone=contact_phone))
    if support_phone is not None:
        db.add(TenantSettings(tenant_id=TENANT_ID, support_phone=support_phone))
    db.add(User(id=TUTOR_ID, email="tutor-s3w@test.com", password_hash="x", role="tutor", tenant_id=TENANT_ID))
    db.add(User(id=WALKER_ID, email="walker-s3w@test.com", password_hash="x", role="walker", tenant_id=TENANT_ID))
    db.add(WalkerProfile(id="wp-s3", user_id=WALKER_ID, status="active", active_as_walker=True))
    db.add(TenantWalkerAccess(id="twa-s3", tenant_id=TENANT_ID, walker_user_id=WALKER_ID,
                              status="active", access_type="tenant_exclusive"))
    db.add(TutorProfile(id="tp-s3", user_id=TUTOR_ID, tenant_id=TENANT_ID, city=city, state=state))
    db.add(Pet(id=PET_ID, tutor_id=TUTOR_ID, tenant_id=TENANT_ID, name="Thor", weight=weight))
    for row in _seed_rows():
        db.add(LocalRule(**row))
    db.commit()

    from app.routes import walker as walker_module

    test_app = FastAPI()
    test_app.include_router(walker_module.router)
    test_app.dependency_overrides[get_db] = lambda: db
    test_app.dependency_overrides[get_walker_self_db] = lambda: db
    test_app.dependency_overrides[get_current_user] = lambda: db.get(User, WALKER_ID)
    return TestClient(test_app, raise_server_exceptions=True), db


def _add_walk(db, walk_id, **kw):
    base = dict(id=walk_id, tutor_id=TUTOR_ID, pet_id=PET_ID, tenant_id=TENANT_ID,
                scheduled_date="2026-09-20T10:00", duration_minutes=45, price=40.0)
    base.update(kw)
    db.add(Walk(**base))
    db.commit()


def _add_pending_offer(db, walk_id="w-req"):
    _add_walk(db, walk_id, walker_id=WALKER_ID, assigned_walker_id=WALKER_ID, status="Aguardando",
              operational_status="pending_walker_confirmation")
    db.add(WalkMatchingAttempt(id=f"att-{walk_id}", walk_id=walk_id, walker_id=WALKER_ID, status="pending",
                               attempt_number=1, expires_at=datetime.utcnow() + timedelta(minutes=15)))
    db.commit()


def test_requests_offer_card_carries_safety_alerts():
    client, db = build()
    _add_pending_offer(db)
    items = client.get("/walker/requests").json()
    assert [a["message"] for a in items[0]["safety_alerts"]] == [EXPECTED]
    assert items[0]["safety_alerts"][0]["norma"].startswith("Lei Municipal nº 9.108/2016")


def test_walker_walks_carry_alerts_only_for_open_walks():
    client, db = build()
    _add_walk(db, "w-open", walker_id=WALKER_ID, status="Agendado", operational_status="walker_accepted")
    _add_walk(db, "w-done", walker_id=WALKER_ID, status="Concluído", operational_status="ride_completed")
    body = {w["id"]: w for w in client.get("/walker/walks").json()}
    assert [a["message"] for a in body["w-open"]["safety_alerts"]] == [EXPECTED]
    assert body["w-done"]["safety_alerts"] == []


def test_active_walk_carries_alerts():
    client, db = build()
    _add_walk(db, "w-active", walker_id=WALKER_ID, status="Passeando agora", operational_status="ride_in_progress")
    body = client.get("/walker/walks/active").json()
    assert [a["message"] for a in body["safety_alerts"]] == [EXPECTED]


def test_dashboard_next_request_carries_alerts():
    client, db = build()
    _add_walk(db, "w-avail", walker_id=None, status="Agendado", operational_status="ride_scheduled")
    body = client.get("/walker/dashboard").json()
    assert [a["message"] for a in body["next_request"]["safety_alerts"]] == [EXPECTED]


def test_city_without_rules_has_empty_alerts():
    client, db = build(city="Feira de Santana")
    _add_pending_offer(db)
    items = client.get("/walker/requests").json()
    assert items[0]["safety_alerts"] == []


# --------------------------------------------------------------- TAREFA EXTRA
def test_establishment_support_phone_present_when_configured():
    client, db = build(support_phone="7135999983")
    _add_pending_offer(db)
    items = client.get("/walker/requests").json()
    assert items[0]["establishment_support_phone"]
    assert items[0]["tutor_phone"] == ""


def test_establishment_support_phone_falls_back_to_tenant_contact_phone():
    client, db = build(contact_phone="7135999983")
    _add_walk(db, "w-open", walker_id=WALKER_ID, status="Agendado", operational_status="walker_accepted")
    body = {w["id"]: w for w in client.get("/walker/walks").json()}
    assert body["w-open"]["establishment_support_phone"]


def test_establishment_support_phone_none_when_not_configured():
    client, db = build()
    _add_walk(db, "w-active", walker_id=WALKER_ID, status="Passeando agora", operational_status="ride_in_progress")
    body = client.get("/walker/walks/active").json()
    assert body["establishment_support_phone"] is None
    # walker_active_walk usa serialize_operational_walk (não _walk_payload): não expõe
    # nenhum campo de telefone do tutor — nada a mascarar.
    assert "tutor_phone" not in body


# ------------------------------------------------------------- S3-3 (cache) --
def test_walker_walks_reuses_tenant_phone_and_tutor_location_across_walks(monkeypatch):
    """/walker/walks com vários passeios do MESMO tenant/tutor só consulta
    telefone do tenant e localização do tutor 1x cada (cache por request)."""
    client, db = build(support_phone="7135999983")
    _add_walk(db, "w1", walker_id=WALKER_ID, status="Agendado", operational_status="walker_accepted")
    _add_walk(db, "w2", walker_id=WALKER_ID, status="Agendado", operational_status="walker_arriving")
    _add_walk(db, "w3", walker_id=WALKER_ID, status="Agendado", operational_status="ride_scheduled")

    from app.routes import walker as walker_module
    from app.services import local_rules_service as lrs

    phone_calls = {"n": 0}
    tutor_loc_calls = {"n": 0}
    real_resolve_phone = walker_module.resolve_tenant_support_phone
    real_tutor_loc = lrs._tutor_profile_location

    def counting_resolve_phone(db_, tenant_id):
        phone_calls["n"] += 1
        return real_resolve_phone(db_, tenant_id)

    def counting_tutor_loc(db_, tutor_id, *, cache=None):
        if cache is None or ("tutor_loc", tutor_id) not in cache:
            tutor_loc_calls["n"] += 1
        return real_tutor_loc(db_, tutor_id, cache=cache)

    monkeypatch.setattr(walker_module, "resolve_tenant_support_phone", counting_resolve_phone)
    monkeypatch.setattr(lrs, "_tutor_profile_location", counting_tutor_loc)

    body = {w["id"]: w for w in client.get("/walker/walks").json()}
    assert len(body) == 3
    assert all(w["establishment_support_phone"] for w in body.values())
    assert phone_calls["n"] == 1
    assert tutor_loc_calls["n"] == 1
