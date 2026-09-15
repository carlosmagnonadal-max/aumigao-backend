"""S1 — Botão de Emergência: POST /walker/walks/{walk_id}/emergency.

Padrão: FastAPI mínimo + SQLite em memória (StaticPool), sem importar app.main
(mesmo padrão de test_walker_mid_walk_actions.py). Push real nunca sai.

Critérios da spec §6: aciona só em custódia (403 fora); só o passeador designado;
rate-limit; evento + push gravados; payloads normais sem telefone do tutor; modo
direto devolve E.164. Mais: modo mascarado via adaptador, fallback e resiliência.
"""
from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 — registra todas as tabelas no metadata
from app.core.database import Base, get_db, get_walker_self_db
from app.dependencies.auth import get_current_user
from app.models.notification import Notification
from app.models.pet import Pet
from app.models.tenant import Tenant, TenantSettings
from app.models.tutor_profile import TutorProfile
from app.models.user import User
from app.models.walk import Walk
from app.models.walk_emergency_call import WalkEmergencyCall
from app.models.walk_operational_event import WalkOperationalEvent
from app.models.walker_profile import WalkerProfile
from app.routes import walker as walker_routes
from app.services.telephony import BridgeCallResult

TENANT_ID = "tenant-emg"
WALKER_ID = "walker-emg-1"
OTHER_WALKER_ID = "walker-emg-2"
TUTOR_ID = "tutor-emg-1"
ADMIN_ID = "admin-emg-1"
PET_ID = "pet-emg-1"
WALK_ID = "walk-emg-1"
URL = f"/walker/walks/{WALK_ID}/emergency"
TUTOR_PHONE_RAW = "(71) 98888-7777"
TUTOR_PHONE_E164 = "+5571988887777"
TUTOR_PHONE_FRAGMENT = "988887777"


def _seed(db):
    db.add(Tenant(id=TENANT_ID, name="Tenant Emg", slug="tenant-emg"))
    db.add(TenantSettings(id="ts-emg", tenant_id=TENANT_ID, support_phone="(71) 3599-9983"))
    for uid, email, role, name in [
        (WALKER_ID, "walker-emg@test.com", "walker", "Walker Emg"),
        (OTHER_WALKER_ID, "walker-emg2@test.com", "walker", "Walker Dois"),
        (TUTOR_ID, "tutor-emg@test.com", "tutor", "Tutor Emg"),
        (ADMIN_ID, "admin-emg@test.com", "admin", "Admin Emg"),
    ]:
        db.add(User(id=uid, email=email, password_hash="x", role=role, full_name=name, tenant_id=TENANT_ID))
    for pid, uid in [("wp-emg-1", WALKER_ID), ("wp-emg-2", OTHER_WALKER_ID)]:
        db.add(WalkerProfile(
            id=pid, user_id=uid, status="active", active_as_walker=True,
            full_name="Walker", phone="(71) 99999-0000",
        ))
    db.add(TutorProfile(id="tp-emg", user_id=TUTOR_ID, tenant_id=TENANT_ID, phone=TUTOR_PHONE_RAW))
    db.add(Pet(
        id=PET_ID, name="Rex", species="Cachorro", tutor_id=TUTOR_ID, tenant_id=TENANT_ID,
        vet_name="Clínica Vet Bahia", vet_phone="(71) 3333-4444",
    ))
    db.commit()


def _add_walk(db, op_status: str, walker_id: str = WALKER_ID) -> Walk:
    walk = Walk(
        id=WALK_ID, tutor_id=TUTOR_ID, walker_id=walker_id, pet_id=PET_ID, tenant_id=TENANT_ID,
        scheduled_date="2026-09-15T10:00:00", duration_minutes=30, price=50.0,
        status="Passeando agora", operational_status=op_status,
    )
    db.add(walk)
    db.commit()
    return walk


def _client(db, user_id: str = WALKER_ID) -> TestClient:
    test_app = FastAPI()
    test_app.include_router(walker_routes.router)
    test_app.dependency_overrides[get_db] = lambda: db
    test_app.dependency_overrides[get_walker_self_db] = lambda: db
    test_app.dependency_overrides[get_current_user] = lambda: db.get(User, user_id)
    return TestClient(test_app)


@pytest.fixture()
def db(monkeypatch):
    monkeypatch.delenv("TELEPHONY_PROVIDER", raising=False)
    monkeypatch.setattr(
        "app.routes.notifications.send_push_for_notification_background",
        lambda *args, **kwargs: None,
    )
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    _seed(session)
    yield session
    session.close()


class _FakeProvider:
    name = "fake"

    def __init__(self, result: BridgeCallResult):
        self.result = result
        self.calls: list[tuple] = []

    def is_enabled(self) -> bool:
        return True

    def start_bridge_call(self, walker_e164, tutor_e164, caller_id):
        self.calls.append((walker_e164, tutor_e164, caller_id))
        return self.result


# ── Modo direto / custódia ───────────────────────────────────────────────────

def test_direct_mode_returns_tutor_e164_during_ride(db):
    _add_walk(db, "ride_in_progress")
    resp = _client(db).post(URL, json={"reason": "pet_mal"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["mode"] == "direct"
    assert body["status"] == "initiated"
    assert body["tutor_phone_e164"] == TUTOR_PHONE_E164
    assert body["tenant_support_phone"] == "+557135999983"
    assert body["pet_vet_name"] == "Clínica Vet Bahia"
    assert body["pet_vet_phone"] == "(71) 3333-4444"
    assert body["call_id"]
    assert body["walk_id"] == WALK_ID
    assert body["rate_limited"] is False
    assert body["persisted"] is True


def test_pet_handover_confirmed_counts_as_custody(db):
    _add_walk(db, "pet_handover_confirmed")
    resp = _client(db).post(URL, json={})
    assert resp.status_code == 200, resp.text
    assert resp.json()["mode"] == "direct"


def test_empty_body_is_accepted(db):
    _add_walk(db, "ride_in_progress")
    resp = _client(db).post(URL)
    assert resp.status_code == 200, resp.text


@pytest.mark.parametrize(
    "op_status",
    ["walker_accepted", "ride_scheduled", "walker_arriving", "awaiting_completion_review",
     "completion_rejected", "ride_completed", "ride_cancelled"],
)
def test_outside_custody_is_forbidden_and_records_nothing(db, op_status):
    _add_walk(db, op_status)
    resp = _client(db).post(URL, json={})
    assert resp.status_code == 403, resp.text
    assert db.query(WalkEmergencyCall).count() == 0
    assert db.query(WalkOperationalEvent).filter_by(event_type="emergency_call").count() == 0


def test_only_designated_walker_can_trigger(db):
    _add_walk(db, "ride_in_progress", walker_id=WALKER_ID)
    resp = _client(db, user_id=OTHER_WALKER_ID).post(URL, json={})
    assert resp.status_code == 403, resp.text
    assert db.query(WalkEmergencyCall).count() == 0


def test_walk_not_found_returns_404(db):
    resp = _client(db).post("/walker/walks/nao-existe/emergency", json={})
    assert resp.status_code == 404


def test_invalid_reason_returns_422(db):
    _add_walk(db, "ride_in_progress")
    resp = _client(db).post(URL, json={"reason": "qualquer"})
    assert resp.status_code == 422


# ── Registro, evento e notificações ─────────────────────────────────────────

def test_persists_call_event_and_notifications_without_phone(db):
    _add_walk(db, "ride_in_progress")
    body = _client(db).post(URL, json={"reason": "fuga"}).json()

    call = db.get(WalkEmergencyCall, body["call_id"])
    assert call is not None
    assert call.tenant_id == TENANT_ID
    assert call.walker_user_id == WALKER_ID
    assert call.tutor_user_id == TUTOR_ID
    assert call.reason == "fuga"
    assert call.mode == "direct"
    assert call.provider == "none"
    assert call.status == "initiated"

    event = db.query(WalkOperationalEvent).filter_by(walk_id=WALK_ID, event_type="emergency_call").one()
    assert event.severity == "high"
    assert "fuga" in event.notes
    assert TUTOR_PHONE_FRAGMENT not in event.notes

    tutor_notif = db.query(Notification).filter_by(user_id=TUTOR_ID, type="walk_emergency").one()
    assert tutor_notif.related_entity_type == "walk"
    assert tutor_notif.related_entity_id == WALK_ID
    assert "Rex" in tutor_notif.title
    assert TUTOR_PHONE_FRAGMENT not in tutor_notif.message

    admin_notif = db.query(Notification).filter_by(user_id=ADMIN_ID, type="walk_emergency_admin").one()
    assert admin_notif.related_entity_id == WALK_ID
    assert TUTOR_PHONE_FRAGMENT not in admin_notif.message


# ── Rate-limit ───────────────────────────────────────────────────────────────

def test_rate_limit_fourth_call_returns_last_without_recording(db):
    _add_walk(db, "ride_in_progress")
    client = _client(db)
    ids = [client.post(URL, json={}).json()["call_id"] for _ in range(3)]
    assert len(set(ids)) == 3

    fourth = client.post(URL, json={"reason": "outro"})
    assert fourth.status_code == 200, fourth.text
    body = fourth.json()
    latest = (
        db.query(WalkEmergencyCall)
        .filter_by(walk_id=WALK_ID)
        .order_by(WalkEmergencyCall.created_at.desc())
        .first()
    )
    assert body["rate_limited"] is True
    assert body["call_id"] == latest.id
    assert body["tutor_phone_e164"] == TUTOR_PHONE_E164
    assert db.query(WalkEmergencyCall).filter_by(walk_id=WALK_ID).count() == 3
    assert db.query(WalkOperationalEvent).filter_by(walk_id=WALK_ID, event_type="emergency_call").count() == 3
    assert db.query(Notification).filter_by(user_id=TUTOR_ID, type="walk_emergency").count() == 3


def test_rate_limit_window_expires_after_ten_minutes(db):
    _add_walk(db, "ride_in_progress")
    old = datetime.utcnow() - timedelta(minutes=11)
    for i in range(3):
        db.add(WalkEmergencyCall(
            id=f"old-{i}", tenant_id=TENANT_ID, walk_id=WALK_ID, walker_user_id=WALKER_ID,
            tutor_user_id=TUTOR_ID, mode="direct", provider="none", status="initiated", created_at=old,
        ))
    db.commit()
    body = _client(db).post(URL, json={}).json()
    assert body["rate_limited"] is False
    assert db.query(WalkEmergencyCall).filter_by(walk_id=WALK_ID).count() == 4


# ── Sem telefone / modo mascarado / resiliência ──────────────────────────────

def test_unavailable_when_tutor_has_no_valid_phone(db):
    _add_walk(db, "ride_in_progress")
    profile = db.query(TutorProfile).filter_by(user_id=TUTOR_ID).one()
    profile.phone = ""
    db.commit()
    body = _client(db).post(URL, json={}).json()
    assert body["mode"] == "unavailable"
    assert body["tutor_phone_e164"] is None
    assert body["tenant_support_phone"] == "+557135999983"
    call = db.get(WalkEmergencyCall, body["call_id"])
    assert call.status == "failed"
    assert call.fallback_reason == "tutor_phone_missing"


def test_masked_mode_when_provider_bridges(db, monkeypatch):
    _add_walk(db, "ride_in_progress")
    fake = _FakeProvider(BridgeCallResult(ok=True, provider_call_id="prov-123"))
    monkeypatch.setattr("app.services.walk_emergency_service.get_telephony_provider", lambda: fake)
    body = _client(db).post(URL, json={}).json()
    assert body["mode"] == "masked"
    assert body["tutor_phone_e164"] is None
    assert fake.calls and fake.calls[0][0] == "+5571999990000"
    assert fake.calls[0][1] == TUTOR_PHONE_E164
    call = db.get(WalkEmergencyCall, body["call_id"])
    assert call.provider == "fake"
    assert call.provider_call_id == "prov-123"
    assert call.status == "initiated"


def test_masked_failure_falls_back_to_direct(db, monkeypatch):
    _add_walk(db, "ride_in_progress")
    fake = _FakeProvider(BridgeCallResult(ok=False, error="busy"))
    monkeypatch.setattr("app.services.walk_emergency_service.get_telephony_provider", lambda: fake)
    body = _client(db).post(URL, json={}).json()
    assert body["mode"] == "direct"
    assert body["status"] == "fallback_direct"
    assert body["tutor_phone_e164"] == TUTOR_PHONE_E164
    assert db.get(WalkEmergencyCall, body["call_id"]).fallback_reason == "busy"


def test_notification_failure_does_not_block_dial(db, monkeypatch):
    _add_walk(db, "ride_in_progress")

    def _boom(*args, **kwargs):
        raise RuntimeError("push fora do ar")

    monkeypatch.setattr("app.services.walk_emergency_service._notify_tutor", _boom)
    resp = _client(db).post(URL, json={})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["tutor_phone_e164"] == TUTOR_PHONE_E164
    assert body["persisted"] is True
    assert db.query(WalkEmergencyCall).count() == 1


# ── Privacidade: payloads normais continuam sem telefone ─────────────────────

def test_normal_walker_payloads_never_expose_tutor_phone(db):
    walk = _add_walk(db, "ride_in_progress")
    client = _client(db)
    assert client.post(URL, json={}).status_code == 200
    active = client.get("/walker/walks/active")
    assert active.status_code == 200, active.text
    assert TUTOR_PHONE_FRAGMENT not in active.text
    db.refresh(walk)
    assert walker_routes._walk_payload(walk, db)["tutor_phone"] == ""
