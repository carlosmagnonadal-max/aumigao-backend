from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import dotenv_values
from pymongo import MongoClient


# Module coverage: iteration 65 retest for walkers/tasks bugfixes + auth hardening playbook checks.

ALLOWED_WALK_STATUSES = {
    "Agendado",
    "Indo buscar o pet",
    "Passeando agora",
    "Finalizado",
    "Cancelado",
    "Não comparecimento do cliente",
    "Não comparecimento do passeador",
    "Pendente de análise",
}


def _mongo_db():
    backend_env = Path("/app/backend/.env")
    values = dotenv_values(backend_env) if backend_env.exists() else {}
    mongo_url = os.environ.get("MONGO_URL") or values.get("MONGO_URL")
    db_name = os.environ.get("DB_NAME") or values.get("DB_NAME")
    assert mongo_url and db_name, "MONGO_URL/DB_NAME ausentes"
    client = MongoClient(str(mongo_url).strip().strip('"'))
    return client, client[str(db_name).strip().strip('"')]


def _login(base_url: str, email: str, password: str, with_auth_header: bool = True) -> tuple[requests.Session, requests.Response]:
    session = requests.Session()
    response = session.post(
        f"{base_url}/api/auth/login",
        json={"email": email, "password": password},
        timeout=30,
    )
    if response.status_code == 200 and with_auth_header:
        token = (response.json() or {}).get("access_token")
        assert token, "access_token ausente no login"
        session.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/json"})
    return session, response


def _checklist_payload() -> dict[str, bool]:
    return {
        "checklist_confirm_water": True,
        "checklist_confirm_bowl": True,
        "checklist_confirm_bags": True,
        "checklist_confirm_first_aid": True,
    }


def test_auth_login_sets_http_only_cookies(base_url: str):
    session, response = _login(base_url, "admin@petpasso.com", "Admin@123", with_auth_header=False)
    try:
        assert response.status_code == 200, response.text
        set_cookie = response.headers.get("set-cookie", "")
        assert "HttpOnly" in set_cookie
        assert "access_token=" in set_cookie
        assert "refresh_token=" in set_cookie
    finally:
        session.close()


def test_cors_preflight_allows_credentials_with_explicit_origin(base_url: str):
    origin = "https://petpasso-mvp.preview.emergentagent.com"
    response = requests.options(
        f"{base_url}/api/auth/login",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
        timeout=30,
    )
    assert response.status_code in {200, 204}
    assert response.headers.get("Access-Control-Allow-Credentials") == "true"
    assert response.headers.get("Access-Control-Allow-Origin") == origin


def test_auth_bruteforce_lockout_after_five_failures(base_url: str):
    fake_email = f"lockout_test_{uuid.uuid4().hex[:8]}@petpasso.com"
    headers = {"X-Forwarded-For": f"203.0.113.{int(uuid.uuid4().hex[:2], 16) % 200 + 20}"}
    statuses = []
    for _ in range(6):
        response = requests.post(
            f"{base_url}/api/auth/login",
            json={"email": fake_email, "password": "WrongPass123!"},
            headers=headers,
            timeout=30,
        )
        statuses.append(response.status_code)
    assert statuses[:5] == [401, 401, 401, 401, 401]
    assert statuses[5] == 429


def test_seed_users_have_bcrypt_hash_prefix_2b():
    client, db = _mongo_db()
    try:
        for email in ["superadmin@petpasso.com", "admin@petpasso.com", "walker@petpasso.com"]:
            row = db.users.find_one({"email": email}, {"_id": 0, "password_hash": 1})
            assert row and isinstance(row.get("password_hash"), str)
            assert row["password_hash"].startswith("$2b$")
    finally:
        client.close()


def test_seed_admin_credentials_login_success(base_url: str):
    for email, password in [
        ("superadmin@petpasso.com", "SuperAdmin@123"),
        ("admin@petpasso.com", "Admin@123"),
    ]:
        session, response = _login(base_url, email, password, with_auth_header=False)
        try:
            assert response.status_code == 200, f"{email}: {response.status_code} {response.text}"
            body = response.json()
            assert body.get("user", {}).get("email") == email
        finally:
            session.close()


def test_walkers_contract_includes_kit_complete(base_url: str):
    client, response = _login(base_url, "cliente@petpasso.com", "Cliente@123")
    try:
        assert response.status_code == 200, response.text
        walkers_response = client.get(
            f"{base_url}/api/walkers",
            params={
                "date": (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d"),
                "duration_minutes": 30,
                "tipo_passeio": "padrao",
            },
            timeout=30,
        )
        assert walkers_response.status_code == 200, walkers_response.text
        rows = walkers_response.json()
        assert isinstance(rows, list) and rows
        assert "kit_complete" in rows[0]
    finally:
        client.close()


def test_walker_tasks_normalizes_legacy_aceito_status(base_url: str):
    walker_session, login_response = _login(base_url, "walker@petpasso.com", "Walker@123")
    mongo_client, mongo_db = _mongo_db()
    inserted_id = f"TEST_iter65_aceito_{uuid.uuid4().hex[:8]}"
    try:
        assert login_response.status_code == 200, login_response.text
        me = walker_session.get(f"{base_url}/api/auth/me", timeout=30)
        assert me.status_code == 200, me.text
        walker = me.json()

        now = datetime.now(timezone.utc)
        walk_datetime = (now + timedelta(minutes=15)).replace(microsecond=0)
        walk_row = {
            "id": inserted_id,
            "pet_name": "TEST Pet Legacy",
            "client_name": "TEST Cliente Legacy",
            "walk_date": walk_datetime.strftime("%Y-%m-%d"),
            "walk_time": walk_datetime.strftime("%H:%M"),
            "duration_minutes": 30,
            "walker_id": "walker-1",
            "walker_user_id": walker.get("id"),
            "walker_name": walker.get("full_name"),
            "status": "Aceito",
            "walk_datetime_iso": walk_datetime.isoformat(),
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
            "pickup_street": "Rua TEST",
            "pickup_number": "100",
            "pickup_neighborhood": "Centro",
            "pickup_complement": "",
            "location_reference": "Teste",
            "security_code": "1234",
        }
        mongo_db.walks.insert_one(walk_row)

        tasks_response = walker_session.get(f"{base_url}/api/walker/tasks", timeout=30)
        assert tasks_response.status_code == 200, tasks_response.text
        tasks = tasks_response.json()
        target = next((item for item in tasks if item.get("id") == inserted_id), None)
        assert target is not None
        assert target.get("status") == "Agendado"
        assert target.get("status") in ALLOWED_WALK_STATUSES
    finally:
        mongo_db.walks.delete_many({"id": inserted_id})
        mongo_client.close()
        walker_session.close()


def test_client_can_validate_checkin_when_pending(base_url: str):
    walker, walker_login = _login(base_url, "walker@petpasso.com", "Walker@123")
    client, client_login = _login(base_url, "cliente@petpasso.com", "Cliente@123")
    try:
        assert walker_login.status_code == 200
        assert client_login.status_code == 200

        tasks_response = walker.get(f"{base_url}/api/walker/tasks", timeout=30)
        assert tasks_response.status_code == 200, tasks_response.text
        tasks = tasks_response.json()
        candidate = next(
            (
                item
                for item in tasks
                if item.get("status") in {"Agendado", "Indo buscar o pet"}
                and not item.get("kit_checklist_check_in_confirmed", False)
            ),
            None,
        )
        if not candidate:
            return

        walk_id = candidate["id"]
        if candidate.get("status") == "Agendado":
            checkin = walker.post(f"{base_url}/api/walks/{walk_id}/check-in", json=_checklist_payload(), timeout=30)
            assert checkin.status_code == 200, checkin.text

        validate = client.post(
            f"{base_url}/api/walks/{walk_id}/kit-checklist/check-in-validate",
            json=_checklist_payload(),
            timeout=30,
        )
        assert validate.status_code == 200, validate.text
        assert validate.json().get("kit_checklist_check_in_confirmed") is True
    finally:
        walker.close()
        client.close()