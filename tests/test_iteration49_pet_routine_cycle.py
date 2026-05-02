"""Iteration 49 - Pet routine weekly cycle, CRUD, suggestions, dashboard and auth playbook checks."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from typing import Dict, List, Tuple
from uuid import uuid4

import bcrypt
import pytest
import requests
from dotenv import dotenv_values
from pymongo import MongoClient

sys.path.append("/app/backend")
from server import _compute_pet_routine_progress_from_walks


# Environment and auth session helpers for Cliente/Admin/Passeador testing.
def _base_url() -> str:
    env_url = (Path("/app/frontend/.env"), dotenv_values("/app/frontend/.env"))
    values = env_url[1] if env_url[0].exists() else {}
    resolved = str(values.get("EXPO_BACKEND_URL") or values.get("EXPO_PUBLIC_BACKEND_URL") or "").strip().rstrip("/")
    if not resolved:
        raise RuntimeError("EXPO_BACKEND_URL não configurada no frontend/.env")
    return resolved


def _login(email: str, password: str) -> Tuple[requests.Session, requests.Response]:
    session = requests.Session()
    response = session.post(
        f"{_base_url()}/api/auth/login",
        json={"email": email, "password": password},
        timeout=30,
    )
    if response.ok:
        token = response.json().get("access_token")
        if token:
            session.headers.update({"Authorization": f"Bearer {token}"})
    return session, response


def _mongo_db():
    backend_env = dotenv_values("/app/backend/.env")
    mongo_url = str(backend_env.get("MONGO_URL") or "").strip().strip('"')
    db_name = str(backend_env.get("DB_NAME") or "").strip().strip('"')
    if not mongo_url or not db_name:
        pytest.skip("MONGO_URL/DB_NAME indisponíveis para validação")
    client = MongoClient(mongo_url)
    return client, client[db_name]


def _ensure_test_pet(client_session: requests.Session) -> Dict:
    pets_response = client_session.get(f"{_base_url()}/api/pets", timeout=30)
    assert pets_response.status_code == 200, pets_response.text
    pets = pets_response.json()
    reusable = next((p for p in pets if str(p.get("pet_name", "")).startswith("TEST_R49_")), None)
    if reusable:
        return reusable

    payload = {
        "pet_name": f"TEST_R49_{uuid4().hex[:8]}",
        "behavioral_notes": "TEST rotina semanal",
        "photo_url": "",
        "owner_name": "TEST Owner",
        "gets_along_with_dogs": True,
        "accepts_shared_walk": True,
        "pet_size": "Médio",
        "energy_level": "Médio",
        "pulls_leash": False,
        "dog_behavior": "Neutro",
    }
    create = client_session.post(f"{_base_url()}/api/pets", json=payload, timeout=30)
    assert create.status_code == 201, create.text
    return create.json()


def _find_available_slot(client_session: requests.Session, duration: int = 30) -> Tuple[str, str, str]:
    for offset in range(1, 8):
        walk_date = (datetime.now(timezone.utc) + timedelta(days=offset)).strftime("%Y-%m-%d")
        walkers = client_session.get(
            f"{_base_url()}/api/walkers",
            params={"date": walk_date, "duration_minutes": duration},
            timeout=30,
        )
        if walkers.status_code != 200:
            continue
        for walker in walkers.json():
            walker_id = walker.get("id")
            if not walker_id:
                continue
            slots = client_session.get(
                f"{_base_url()}/api/walkers/{walker_id}/availability-slots",
                params={"date": walk_date, "duration_minutes": duration},
                timeout=30,
            )
            if slots.status_code != 200:
                continue
            slot_list = list(slots.json().get("available_slots") or [])
            if slot_list:
                return walker_id, walk_date, slot_list[0]
    pytest.skip("Sem horário disponível para criar passeio de teste")


@pytest.fixture(scope="module")
def client_session():
    session, response = _login("cliente@petpasso.com", "Cliente@123")
    if response.status_code != 200:
        pytest.skip(f"Login cliente indisponível: {response.status_code}")
    yield session
    session.close()


@pytest.fixture(scope="module")
def admin_session():
    session, response = _login("superadmin@petpasso.com", "SuperAdmin@123")
    if response.status_code != 200:
        pytest.skip(f"Login super admin indisponível: {response.status_code}")
    yield session
    session.close()


# Auth playbook validations: bcrypt prefix, cookies httpOnly, CORS explicit origin, lockout and seed-password coherence.
def test_auth_login_sets_httponly_cookies_and_cors_explicit_origin():
    response = requests.options(
        f"{_base_url()}/api/auth/login",
        headers={
            "Origin": "https://petpasso-mvp.preview.emergentagent.com",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
        timeout=20,
    )
    assert response.status_code in (200, 204)
    assert response.headers.get("Access-Control-Allow-Credentials", "").lower() == "true"
    assert response.headers.get("Access-Control-Allow-Origin") == "https://petpasso-mvp.preview.emergentagent.com"

    session, login = _login("admin@petpasso.com", "Admin@123")
    try:
        assert login.status_code == 200, login.text
        cookie_header = (login.headers.get("set-cookie") or "").lower()
        assert "access_token=" in cookie_header
        assert "refresh_token=" in cookie_header
        assert "httponly" in cookie_header
    finally:
        session.close()


def test_auth_lockout_after_five_failures():
    email = f"lockout_{uuid4().hex[:10]}@example.com"
    session = requests.Session()
    try:
        for _ in range(5):
            failed = session.post(
                f"{_base_url()}/api/auth/login",
                json={"email": email, "password": "WrongPass@123"},
                timeout=20,
            )
            assert failed.status_code == 401

        locked = session.post(
            f"{_base_url()}/api/auth/login",
            json={"email": email, "password": "WrongPass@123"},
            timeout=20,
        )
        assert locked.status_code == 429
    finally:
        session.close()


def test_auth_seed_admin_hash_is_bcrypt_and_matches_env_password():
    backend_env = dotenv_values("/app/backend/.env")
    expected_admin_password = str(backend_env.get("ADMIN_PASSWORD") or "").strip().strip('"')
    if not expected_admin_password:
        pytest.skip("ADMIN_PASSWORD ausente no backend/.env")

    mongo_client, database = _mongo_db()
    try:
        admin_row = database.users.find_one({"email": "admin@petpasso.com"}, {"_id": 0, "password_hash": 1})
        assert admin_row is not None
        admin_hash = str(admin_row.get("password_hash") or "")
        assert admin_hash.startswith("$2b$")
        assert bcrypt.checkpw(expected_admin_password.encode("utf-8"), admin_hash.encode("utf-8"))
    finally:
        mongo_client.close()


# Pet routine API CRUD, suggestions and dashboard contract checks.
def test_pet_routine_crud_pause_reactivate_and_suggestions_without_auto_create_walk(client_session: requests.Session):
    pet = _ensure_test_pet(client_session)
    pet_id = pet["id"]

    list_before = client_session.get(f"{_base_url()}/api/pet-routines", timeout=30)
    assert list_before.status_code == 200, list_before.text
    existing = next((r for r in list_before.json() if r.get("pet_id") == pet_id), None)

    if existing:
        routine_id = existing["id"]
    else:
        create_payload = {
            "pet_id": pet_id,
            "frequencia_semanal": 2,
            "dias_preferenciais": ["seg", "qua", "sex"],
            "horario_preferencial": "09:00",
            "duracao_passeio": 30,
        }
        created = client_session.post(f"{_base_url()}/api/pet-routines", json=create_payload, timeout=30)
        assert created.status_code == 200, created.text
        routine_id = created.json()["id"]

    edited = client_session.patch(
        f"{_base_url()}/api/pet-routines/{routine_id}",
        json={"frequencia_semanal": 3, "dias_preferenciais": ["seg", "qua", "sex"], "horario_preferencial": "10:00"},
        timeout=30,
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["frequencia_semanal"] == 3
    assert edited.json()["horario_preferencial"] == "10:00"

    paused = client_session.post(f"{_base_url()}/api/pet-routines/{routine_id}/pause", timeout=30)
    assert paused.status_code == 200, paused.text
    assert paused.json()["is_active"] is False

    reactivated = client_session.post(f"{_base_url()}/api/pet-routines/{routine_id}/reactivate", timeout=30)
    assert reactivated.status_code == 200, reactivated.text
    assert reactivated.json()["is_active"] is True

    walks_before = client_session.get(f"{_base_url()}/api/walks", timeout=30)
    assert walks_before.status_code == 200, walks_before.text
    walks_count_before = len(walks_before.json())

    suggestions = client_session.get(f"{_base_url()}/api/pet-routines/{routine_id}/suggestions", timeout=30)
    assert suggestions.status_code == 200, suggestions.text
    suggestions_payload = suggestions.json()
    assert isinstance(suggestions_payload, list)
    assert len(suggestions_payload) <= 7

    walks_after = client_session.get(f"{_base_url()}/api/walks", timeout=30)
    assert walks_after.status_code == 200, walks_after.text
    assert len(walks_after.json()) == walks_count_before


def test_pet_routine_dashboard_has_required_progress_fields(client_session: requests.Session):
    response = client_session.get(f"{_base_url()}/api/pet-routine/dashboard", timeout=30)
    assert response.status_code == 200, response.text
    payload = response.json()
    progress = payload.get("progress") or {}

    required = {
        "current_streak",
        "best_streak",
        "total_ciclos_cumpridos",
        "total_ciclos_perdidos",
        "total_passeios_realizados_no_periodo",
        "taxa_cumprimento_rotina",
        "ultimo_passeio_valido_em_rotina",
        "proximo_passeio_esperado",
    }
    assert required.issubset(set(progress.keys()))


# Weekly-cycle streak + tolerance and auto-update/recalculate integration checks.
def test_weekly_cycle_streak_uses_preferred_days_with_one_day_tolerance_method_level():
    now = datetime.now(timezone.utc)
    current_week_start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    previous_week_start = current_week_start - timedelta(days=7)
    created_at = previous_week_start - timedelta(days=1)

    routine = {
        "id": f"test-routine-{uuid4().hex[:6]}",
        "user_id": "user-test",
        "pet_id": "pet-test",
        "frequencia_semanal": 2,
        "dias_preferenciais": ["seg", "qua"],
        "horario_preferencial": "09:00",
        "duracao_passeio": 30,
        "is_active": True,
        "created_at": created_at.isoformat(),
        "config_history": [
            {
                "effective_from": created_at.isoformat(),
                "effective_to": None,
                "action": "created",
                "frequencia_semanal": 2,
                "dias_preferenciais": ["seg", "qua"],
                "horario_preferencial": "09:00",
                "duracao_passeio": 30,
                "is_active": True,
            }
        ],
    }

    def walk_doc(at_dt: datetime) -> Dict:
        return {
            "id": f"walk-{uuid4().hex[:6]}",
            "pet_id": "pet-test",
            "pet_ids": ["pet-test"],
            "client_user_id": "user-test",
            "participant_user_ids": ["user-test"],
            "client_name": "Cliente Teste",
            "status": "Finalizado",
            "walk_date": at_dt.strftime("%Y-%m-%d"),
            "walk_time": at_dt.strftime("%H:%M"),
        }

    # Previous week: Mon + Thu (Thu is +1 tolerance for Wed), current week same pattern.
    walks = [
        walk_doc(previous_week_start + timedelta(days=0, hours=9)),
        walk_doc(previous_week_start + timedelta(days=3, hours=9)),
        walk_doc(current_week_start + timedelta(days=0, hours=9)),
        walk_doc(current_week_start + timedelta(days=3, hours=9)),
    ]

    progress = _compute_pet_routine_progress_from_walks(routine=routine, walks=walks, pet_name="TEST")
    assert progress["current_streak"] >= 2
    assert progress["best_streak"] >= 2


def test_routine_progress_auto_updates_after_walk_finalizado(client_session: requests.Session, admin_session: requests.Session):
    me = client_session.get(f"{_base_url()}/api/auth/me", timeout=30)
    assert me.status_code == 200, me.text
    me_payload = me.json()
    user_id = me_payload["id"]

    pet = _ensure_test_pet(client_session)
    walker_id, walk_date, walk_time = _find_available_slot(client_session, duration=30)

    before = admin_session.get(f"{_base_url()}/api/pet-routine/progress", params={"user_id": user_id, "pet_id": pet["id"]}, timeout=35)
    assert before.status_code == 200, before.text
    before_total = int(before.json().get("total_passeios_realizados_no_periodo") or 0)

    created = client_session.post(
        f"{_base_url()}/api/walks",
        json={
            "pet_name": pet["pet_name"],
            "pet_id": pet["id"],
            "client_name": me_payload.get("full_name") or "Cliente Demo",
            "walk_date": walk_date,
            "walk_time": walk_time,
            "duration_minutes": 30,
            "walk_type": "Individual",
            "walker_id": walker_id,
            "pickup_street": "Rua TEST",
            "pickup_number": "100",
            "pickup_neighborhood": "Centro",
            "pickup_complement": "",
            "location_reference": "Portão TEST",
            "pet_behavior_notes": "TEST",
            "notes": "TEST_R49 rotina auto-update",
        },
        timeout=40,
    )
    assert created.status_code == 201, created.text
    walk_id = created.json()["id"]

    for status in ["Indo buscar o pet", "Passeando agora", "Finalizado"]:
        update = admin_session.patch(f"{_base_url()}/api/walks/{walk_id}/status", json={"status": status}, timeout=35)
        assert update.status_code == 200, update.text

    recalc = admin_session.post(
        f"{_base_url()}/api/admin/pet-routine/recalculate",
        json={"user_id": user_id, "pet_id": pet["id"]},
        timeout=40,
    )
    assert recalc.status_code == 200, recalc.text
    assert recalc.json().get("processed_users") == 1

    after = admin_session.get(f"{_base_url()}/api/pet-routine/progress", params={"user_id": user_id, "pet_id": pet["id"]}, timeout=35)
    assert after.status_code == 200, after.text
    after_total = int(after.json().get("total_passeios_realizados_no_periodo") or 0)
    assert after_total >= before_total + 1


def test_walker_role_cannot_access_client_pet_routine_dashboard():
    walker_session, login = _login("passeador@petpasso.com", "Passeador@123")
    try:
        assert login.status_code == 200, login.text
        blocked = walker_session.get(f"{_base_url()}/api/pet-routine/dashboard", timeout=30)
        assert blocked.status_code == 403
    finally:
        walker_session.close()
