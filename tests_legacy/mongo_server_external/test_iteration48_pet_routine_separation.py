"""Iteration 48 - Pet routine separation, auto-refresh flow, and auth safeguards."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple
from uuid import uuid4

import pytest
from dotenv import dotenv_values
from pymongo import MongoClient


# Auth + routine API contract validations for Cliente/Admin/Passeador separation.
def _login(base_url: str, email: str, password: str):
    import requests

    session = requests.Session()
    response = session.post(
        f"{base_url}/api/auth/login",
        json={"email": email, "password": password},
        timeout=25,
    )

    token = None
    if response.ok:
        body = response.json()
        token = body.get("access_token")
        if token:
            session.headers.update({"Authorization": f"Bearer {token}"})

    return session, response


def _client_identity(client_session, base_url: str) -> Dict:
    response = client_session.get(f"{base_url}/api/auth/me", timeout=25)
    assert response.status_code == 200, response.text
    return response.json()


def _pet_routine(client_session, base_url: str, user_id: Optional[str] = None) -> Dict:
    params = {"user_id": user_id} if user_id else None
    response = client_session.get(f"{base_url}/api/pet-routine/progress", params=params, timeout=30)
    assert response.status_code == 200, response.text
    return response.json()


def _find_available_walker_slot(client_session, base_url: str, duration: int = 30) -> Tuple[str, str, str]:
    for day_offset in range(1, 8):
        walk_date = (datetime.now(timezone.utc) + timedelta(days=day_offset)).strftime("%Y-%m-%d")
        walkers_response = client_session.get(
            f"{base_url}/api/walkers",
            params={"date": walk_date, "duration_minutes": duration},
            timeout=30,
        )
        assert walkers_response.status_code == 200, walkers_response.text
        walkers = walkers_response.json()

        for walker in walkers:
            walker_id = walker.get("id")
            if not walker_id:
                continue
            slots_response = client_session.get(
                f"{base_url}/api/walkers/{walker_id}/availability-slots",
                params={"date": walk_date, "duration_minutes": duration},
                timeout=30,
            )
            if slots_response.status_code != 200:
                continue
            slots_payload = slots_response.json()
            slots: List[str] = list(slots_payload.get("available_slots") or [])
            if slots:
                return walker_id, walk_date, slots[0]

    pytest.skip("Nenhum passeador com horário disponível nos próximos 7 dias")


def _create_test_walk(client_session, base_url: str, client_name: str, marker: str) -> Dict:
    walker_id, walk_date, walk_time = _find_available_walker_slot(client_session, base_url, duration=30)
    payload = {
        "pet_name": f"TEST_Rotina_{marker}",
        "client_name": client_name,
        "walk_date": walk_date,
        "walk_time": walk_time,
        "duration_minutes": 30,
        "walk_type": "Individual",
        "walker_id": walker_id,
        "pickup_street": "Rua Teste",
        "pickup_number": "123",
        "pickup_neighborhood": "Centro",
        "pickup_complement": "",
        "location_reference": "Portão azul",
        "pet_behavior_notes": "TEST rotina",
        "notes": "TEST rotina separation",
    }
    response = client_session.post(f"{base_url}/api/walks", json=payload, timeout=35)
    assert response.status_code == 201, response.text
    walk = response.json()
    assert walk["pet_name"].startswith("TEST_Rotina_")
    assert walk["status"] == "Agendado"
    return walk


def _promote_walk_to_walking_now(actor_session, base_url: str, walk_id: str) -> None:
    for next_status in ["Indo buscar o pet", "Passeando agora"]:
        response = actor_session.patch(
            f"{base_url}/api/walks/{walk_id}/status",
            json={"status": next_status},
            timeout=30,
        )
        assert response.status_code == 200, response.text
        assert response.json().get("status") == next_status


def _admin_client_id_by_name(admin_session, base_url: str, name: str) -> str:
    response = admin_session.get(f"{base_url}/api/admin/clients", timeout=30)
    assert response.status_code == 200, response.text
    rows = response.json()
    hit = next((row for row in rows if row.get("name") == name), None)
    if not hit:
        pytest.skip(f"Cliente '{name}' não encontrado no endpoint admin/clients")
    return hit["id"]


def test_auth_login_sets_httponly_cookies(base_url: str):
    session, response = _login(base_url, "admin@petpasso.com", "Admin@123")
    try:
        assert response.status_code == 200, response.text
        set_cookie_values = response.headers.get("set-cookie", "")
        lower_cookie = set_cookie_values.lower()
        assert "access_token=" in lower_cookie
        assert "refresh_token=" in lower_cookie
        assert "httponly" in lower_cookie
    finally:
        session.close()


def test_auth_cors_preflight_allows_credentials_with_explicit_origin(base_url: str):
    import requests

    response = requests.options(
        f"{base_url}/api/auth/login",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
        timeout=20,
    )
    assert response.status_code in (200, 204)
    assert response.headers.get("Access-Control-Allow-Credentials", "").lower() == "true"
    assert response.headers.get("Access-Control-Allow-Origin") == "http://localhost:3000"


def test_auth_bruteforce_lockout_after_five_failures(base_url: str):
    import requests

    session = requests.Session()
    email = f"lockout_{uuid4().hex[:10]}@example.com"
    try:
        for _ in range(5):
            res = session.post(
                f"{base_url}/api/auth/login",
                json={"email": email, "password": "WrongPass@123"},
                timeout=20,
            )
            assert res.status_code == 401

        locked = session.post(
            f"{base_url}/api/auth/login",
            json={"email": email, "password": "WrongPass@123"},
            timeout=20,
        )
        assert locked.status_code == 429
    finally:
        session.close()


def test_auth_seed_admin_hash_uses_bcrypt_2b_prefix():
    backend_env = dotenv_values("/app/backend/.env")
    mongo_url = backend_env.get("MONGO_URL")
    db_name = backend_env.get("DB_NAME")
    if not mongo_url or not db_name:
        pytest.skip("MONGO_URL/DB_NAME indisponíveis para validação de hash")

    client = MongoClient(str(mongo_url).strip().strip('"'))
    try:
        database = client[str(db_name).strip().strip('"')]
        admin_row = database.users.find_one({"email": "admin@petpasso.com"}, {"_id": 0, "password_hash": 1})
        assert admin_row is not None
        assert str(admin_row.get("password_hash", "")).startswith("$2b$")
    finally:
        client.close()


def test_get_pet_routine_progress_schema_has_no_financial_fields(base_url: str):
    client_session, login_response = _login(base_url, "cliente@petpasso.com", "Cliente@123")
    try:
        assert login_response.status_code == 200, login_response.text
        routine = _pet_routine(client_session, base_url)

        expected_keys = {
            "id",
            "user_id",
            "streak_days",
            "best_streak_days",
            "finished_walks_total",
            "finished_walks_week",
            "finished_walks_month",
            "simple_badges",
            "encouragement_message",
            "last_finished_walk_at",
            "updated_at",
        }
        assert expected_keys.issubset(set(routine.keys()))

        forbidden_financial_keys = {
            "earnings",
            "walker_payout",
            "tip_amount",
            "month_earnings",
            "week_earnings",
            "financial_status",
            "revenue",
            "bonus",
            "incentive",
        }
        assert forbidden_financial_keys.isdisjoint(set(routine.keys()))
    finally:
        client_session.close()


def test_walker_cannot_access_pet_routine_progress_endpoint(base_url: str):
    walker_session, login_response = _login(base_url, "passeador@petpasso.com", "Passeador@123")
    try:
        assert login_response.status_code == 200, login_response.text
        response = walker_session.get(f"{base_url}/api/pet-routine/progress", timeout=25)
        assert response.status_code == 403
    finally:
        walker_session.close()


def test_pet_routine_progress_persisted_in_dedicated_collection(base_url: str):
    client_session, login_response = _login(base_url, "cliente@petpasso.com", "Cliente@123")
    try:
        assert login_response.status_code == 200, login_response.text
        client_user = _client_identity(client_session, base_url)
        client_user_id = client_user["id"]

        _pet_routine(client_session, base_url)

        backend_env = dotenv_values("/app/backend/.env")
        mongo_url = backend_env.get("MONGO_URL")
        db_name = backend_env.get("DB_NAME")
        if not mongo_url or not db_name:
            pytest.skip("MONGO_URL/DB_NAME indisponíveis para validar persistência")

        mongo = MongoClient(str(mongo_url).strip().strip('"'))
        try:
            doc = mongo[str(db_name).strip().strip('"')].pet_routine_progress.find_one(
                {"user_id": client_user_id},
                {"_id": 0},
            )
            assert doc is not None
            assert doc.get("id") == f"pet-routine-{client_user_id}"
            assert isinstance(doc.get("streak_days"), int)
        finally:
            mongo.close()
    finally:
        client_session.close()


def test_admin_recalculate_pet_routine_for_specific_user(base_url: str):
    admin_session, admin_login = _login(base_url, "superadmin@petpasso.com", "SuperAdmin@123")
    client_session, client_login = _login(base_url, "cliente@petpasso.com", "Cliente@123")
    try:
        assert admin_login.status_code == 200, admin_login.text
        assert client_login.status_code == 200, client_login.text
        client_user = _client_identity(client_session, base_url)
        client_user_id = client_user["id"]

        response = admin_session.post(
            f"{base_url}/api/admin/pet-routine/recalculate",
            json={"user_id": client_user_id},
            timeout=40,
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["processed_users"] == 1
        assert payload["updated_profiles"] == 1
        assert len(payload["items"]) == 1
        assert payload["items"][0]["user_id"] == client_user_id
    finally:
        admin_session.close()
        client_session.close()


def test_auto_pet_routine_update_after_regular_walk_status_patch(base_url: str):
    admin_session, admin_login = _login(base_url, "superadmin@petpasso.com", "SuperAdmin@123")
    client_session, client_login = _login(base_url, "cliente@petpasso.com", "Cliente@123")
    try:
        assert admin_login.status_code == 200, admin_login.text
        assert client_login.status_code == 200, client_login.text

        client_user = _client_identity(client_session, base_url)
        client_name = client_user["full_name"]
        client_user_id = client_user["id"]

        before = _pet_routine(admin_session, base_url, user_id=client_user_id)
        walk = _create_test_walk(client_session, base_url, client_name, marker="regular")
        _promote_walk_to_walking_now(admin_session, base_url, walk["id"])

        finish = admin_session.patch(
            f"{base_url}/api/walks/{walk['id']}/status",
            json={"status": "Finalizado"},
            timeout=35,
        )
        assert finish.status_code == 200, finish.text
        assert finish.json().get("status") == "Finalizado"

        after = _pet_routine(admin_session, base_url, user_id=client_user_id)
        assert after["finished_walks_total"] >= before["finished_walks_total"] + 1
    finally:
        admin_session.close()
        client_session.close()


def test_auto_pet_routine_update_after_admin_walk_status_patch(base_url: str):
    admin_session, admin_login = _login(base_url, "superadmin@petpasso.com", "SuperAdmin@123")
    client_session, client_login = _login(base_url, "cliente@petpasso.com", "Cliente@123")
    try:
        assert admin_login.status_code == 200, admin_login.text
        assert client_login.status_code == 200, client_login.text

        client_user = _client_identity(client_session, base_url)
        client_name = client_user["full_name"]
        client_user_id = client_user["id"]

        walk = _create_test_walk(client_session, base_url, client_name, marker="admin")
        before = _pet_routine(admin_session, base_url, user_id=client_user_id)

        update = admin_session.patch(
            f"{base_url}/api/admin/walks/{walk['id']}/status",
            json={"status": "Finalizado"},
            timeout=35,
        )
        assert update.status_code == 200, update.text
        assert update.json().get("status") == "Finalizado"

        after = _pet_routine(admin_session, base_url, user_id=client_user_id)
        assert after["finished_walks_total"] >= before["finished_walks_total"] + 1
    finally:
        admin_session.close()
        client_session.close()
