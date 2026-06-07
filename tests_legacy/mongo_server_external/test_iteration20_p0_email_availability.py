import os
import re
import sys
import uuid
import asyncio
from datetime import date, timedelta

import bcrypt
import pytest
import requests
from dotenv import dotenv_values


# Module coverage: P0 email validation + walker availability slots + scheduling restriction + auth playbook checks


CLIENT_CREDS = {"email": "cliente@petpasso.com", "password": "Cliente@123"}


def _login(base_url: str, email: str, password: str) -> requests.Session:
    session = requests.Session()
    session.headers.update({"Accept": "application/json", "Content-Type": "application/json"})
    response = session.post(
        f"{base_url}/api/auth/login",
        json={"email": email, "password": password},
        timeout=25,
    )
    assert response.status_code == 200, response.text
    token = response.json().get("access_token")
    assert token
    session.headers.update({"Authorization": f"Bearer {token}"})
    return session


def _next_date_for_weekday(target_weekday: int) -> str:
    today = date.today()
    delta = (target_weekday - today.weekday()) % 7
    if delta == 0:
        delta = 7
    return (today + timedelta(days=delta)).strftime("%Y-%m-%d")


def _pick_first_walker_with_days(client_session: requests.Session, base_url: str) -> tuple[dict, str]:
    walkers_resp = client_session.get(f"{base_url}/api/walkers", timeout=25)
    assert walkers_resp.status_code == 200, walkers_resp.text
    walkers = walkers_resp.json()
    assert isinstance(walkers, list) and len(walkers) > 0

    weekday_index = {"seg": 0, "ter": 1, "qua": 2, "qui": 3, "sex": 4, "sab": 5, "dom": 6}
    for walker in walkers:
        days = walker.get("availability_days") or []
        if days:
            target = weekday_index[days[0]]
            return walker, _next_date_for_weekday(target)

    pytest.skip("Nenhum passeador com availability_days encontrado")


def _create_test_pet(client_session: requests.Session, base_url: str, suffix: str) -> dict:
    payload = {
        "pet_name": f"TEST_ITER20_PET_{suffix}",
        "behavioral_notes": "TEST behavior",
        "photo_url": "",
        "owner_name": "TEST Owner",
        "gets_along_with_dogs": True,
        "accepts_shared_walk": True,
        "pet_size": "Médio",
        "energy_level": "Médio",
        "pulls_leash": False,
        "dog_behavior": "Neutro",
    }
    created = client_session.post(f"{base_url}/api/pets", json=payload, timeout=25)
    assert created.status_code == 201, created.text
    return created.json()


def test_register_rejects_invalid_email_format(base_url):
    payload = {
        "full_name": "TEST ITER20 Invalid Email",
        "email": "invalido",
        "password": "ValidPass123",
        "role": "cliente",
        "accepted_terms": True,
        "accepted_privacy": True,
        "accepted_lgpd": True,
    }
    response = requests.post(f"{base_url}/api/auth/register", json=payload, timeout=25)
    assert response.status_code == 422


def test_register_rejects_missing_email(base_url):
    payload = {
        "full_name": "TEST ITER20 Missing Email",
        "password": "ValidPass123",
        "role": "cliente",
        "accepted_terms": True,
        "accepted_privacy": True,
        "accepted_lgpd": True,
    }
    response = requests.post(f"{base_url}/api/auth/register", json=payload, timeout=25)
    assert response.status_code == 422


def test_owner_profile_rejects_invalid_email(base_url):
    client_session = _login(base_url, CLIENT_CREDS["email"], CLIENT_CREDS["password"])
    response = client_session.put(
        f"{base_url}/api/owner-profile",
        json={
            "full_name": "TEST ITER20 Owner",
            "phone": "71999999999",
            "email": "invalido",
            "street": "Rua Teste",
            "number": "100",
            "neighborhood": "Pituba",
            "complement": "",
        },
        timeout=25,
    )
    assert response.status_code == 422
    client_session.close()


def test_owner_profile_rejects_missing_email(base_url):
    client_session = _login(base_url, CLIENT_CREDS["email"], CLIENT_CREDS["password"])
    response = client_session.put(
        f"{base_url}/api/owner-profile",
        json={
            "full_name": "TEST ITER20 Owner",
            "phone": "71999999999",
            "street": "Rua Teste",
            "number": "100",
            "neighborhood": "Pituba",
            "complement": "",
        },
        timeout=25,
    )
    assert response.status_code == 422
    client_session.close()


def test_partner_application_rejects_invalid_email(base_url):
    payload = {
        "full_name": "TEST ITER20 Partner",
        "phone": "71988887777",
        "email": "invalido",
        "neighborhood_region": "Pituba",
        "has_pet_experience": True,
        "has_third_party_experience": True,
        "experience_description": "Tenho experiência com cães de médio porte.",
        "availability_days": ["seg", "ter"],
        "availability_start_time": "08:00",
        "availability_end_time": "18:00",
        "profile_photo_url": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAUA",
        "accepted_declaration": True,
    }
    response = requests.post(f"{base_url}/api/partner-applications", json=payload, timeout=25)
    assert response.status_code == 422


def test_partner_application_rejects_empty_availability_days(base_url):
    payload = {
        "full_name": "TEST ITER20 Partner",
        "phone": "71988887777",
        "email": f"test_iter20_{uuid.uuid4().hex[:8]}@petpasso.com",
        "neighborhood_region": "Pituba",
        "has_pet_experience": True,
        "has_third_party_experience": True,
        "experience_description": "Tenho experiência com cães de médio porte.",
        "availability_days": [],
        "availability_start_time": "08:00",
        "availability_end_time": "18:00",
        "profile_photo_url": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAUA",
        "accepted_declaration": True,
    }
    response = requests.post(f"{base_url}/api/partner-applications", json=payload, timeout=25)
    assert response.status_code == 422


def test_partner_application_persists_structured_availability(base_url):
    payload = {
        "full_name": "TEST ITER20 Partner",
        "phone": "71988887777",
        "email": f"test_iter20_{uuid.uuid4().hex[:8]}@petpasso.com",
        "neighborhood_region": "Pituba",
        "has_pet_experience": True,
        "has_third_party_experience": True,
        "experience_description": "Tenho experiência com cães e rotina de passeios.",
        "availability_days": ["seg", "qua", "sex"],
        "availability_start_time": "08:00",
        "availability_end_time": "18:00",
        "profile_photo_url": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAUA",
        "accepted_declaration": True,
    }
    response = requests.post(f"{base_url}/api/partner-applications", json=payload, timeout=25)
    assert response.status_code == 201, response.text

    data = response.json()
    assert data["availability_days"] == ["seg", "qua", "sex"]
    assert data["availability_start_time"] == "08:00"
    assert data["availability_end_time"] == "18:00"
    assert "30" in data["horarios_disponiveis"] and "45" in data["horarios_disponiveis"] and "60" in data["horarios_disponiveis"]


@pytest.mark.parametrize("duration", [30, 45, 60])
def test_availability_slots_endpoint_returns_valid_slots_for_supported_durations(base_url, duration):
    client_session = _login(base_url, CLIENT_CREDS["email"], CLIENT_CREDS["password"])
    walker, target_date = _pick_first_walker_with_days(client_session, base_url)

    response = client_session.get(
        f"{base_url}/api/walkers/{walker['id']}/availability-slots",
        params={"date": target_date, "duration_minutes": duration},
        timeout=25,
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["duration_minutes"] == duration
    assert isinstance(data["available_slots"], list)
    assert len(data["available_slots"]) > 0
    assert all(re.match(r"^\d{2}:\d{2}$", slot) for slot in data["available_slots"])
    client_session.close()


def test_walk_creation_blocks_time_outside_walker_availability(base_url):
    client_session = _login(base_url, CLIENT_CREDS["email"], CLIENT_CREDS["password"])
    pet = _create_test_pet(client_session, base_url, "BLOCK_SLOT")
    walker, target_date = _pick_first_walker_with_days(client_session, base_url)

    slots_response = client_session.get(
        f"{base_url}/api/walkers/{walker['id']}/availability-slots",
        params={"date": target_date, "duration_minutes": 30},
        timeout=25,
    )
    assert slots_response.status_code == 200
    available_slots = slots_response.json().get("available_slots", [])
    assert len(available_slots) > 0

    invalid_slot = "23:45"
    if invalid_slot in available_slots:
        invalid_slot = "00:00"

    create_payload = {
        "pet_name": pet["pet_name"],
        "pet_id": pet["id"],
        "client_name": "TEST ITER20 Client",
        "walk_date": target_date,
        "walk_time": invalid_slot,
        "duration_minutes": 30,
        "walk_type": "Individual",
        "walker_id": walker["id"],
        "pickup_street": "Rua Teste",
        "pickup_number": "120",
        "pickup_neighborhood": "Pituba",
        "pickup_complement": "",
        "location_reference": "Próximo ao mercado",
        "pet_behavior_notes": "",
        "notes": "TEST_ITER20_BLOCK_SLOT",
    }
    create_response = client_session.post(f"{base_url}/api/walks", json=create_payload, timeout=25)
    assert create_response.status_code == 400
    assert "Horário indisponível" in create_response.json().get("detail", "")
    client_session.close()


def test_auth_playbook_http_only_cookie_and_bcrypt_and_lockout(base_url):
    login_response = requests.post(f"{base_url}/api/auth/login", json=CLIENT_CREDS, timeout=25)
    assert login_response.status_code == 200
    set_cookie = login_response.headers.get("set-cookie", "")
    assert "HttpOnly" in set_cookie
    assert "access_token" in set_cookie
    assert "refresh_token" in set_cookie

    brute_force_email = f"test_iter20_lock_{uuid.uuid4().hex[:8]}@petpasso.com"
    for _ in range(5):
        resp = requests.post(
            f"{base_url}/api/auth/login",
            json={"email": brute_force_email, "password": "wrong-pass"},
            timeout=25,
        )
        assert resp.status_code in (401, 429)
    blocked = requests.post(
        f"{base_url}/api/auth/login",
        json={"email": brute_force_email, "password": "wrong-pass"},
        timeout=25,
    )
    assert blocked.status_code == 429

    backend_env = dotenv_values("/app/backend/.env")
    mongo_url = os.environ.get("MONGO_URL") or backend_env.get("MONGO_URL")
    db_name = os.environ.get("DB_NAME") or backend_env.get("DB_NAME")
    if not mongo_url or not db_name:
        pytest.skip("MONGO_URL/DB_NAME não configurados para validar hash bcrypt")

    from pymongo import MongoClient

    client = MongoClient(str(mongo_url).strip().strip('"'))
    try:
        admin = client[str(db_name).strip().strip('"')].users.find_one({"email": "admin@petpasso.com"}, {"password_hash": 1})
        assert admin is not None
        assert str(admin.get("password_hash", "")).startswith("$2b$")
    finally:
        client.close()


def test_auth_playbook_cors_preflight_allows_credentials(base_url):
    response = requests.options(
        f"{base_url}/api/auth/login",
        headers={
            "Origin": base_url,
            "Access-Control-Request-Method": "POST",
        },
        timeout=25,
    )
    assert response.status_code in (200, 204)
    assert response.headers.get("access-control-allow-origin") is not None
    assert response.headers.get("access-control-allow-credentials") in ("true", "True", "TRUE")


def test_auth_playbook_seed_admin_updates_existing_password_hash(base_url):
    backend_env = dotenv_values("/app/backend/.env")
    mongo_url = os.environ.get("MONGO_URL") or backend_env.get("MONGO_URL")
    db_name = os.environ.get("DB_NAME") or backend_env.get("DB_NAME")
    if not mongo_url or not db_name:
        pytest.skip("MONGO_URL/DB_NAME não configurados")

    from pymongo import MongoClient

    mongo_client = MongoClient(str(mongo_url).strip().strip('"'))
    db = mongo_client[str(db_name).strip().strip('"')]

    admin_email = "admin@petpasso.com"
    admin_password = "Admin@123"
    mutated_password_hash = bcrypt.hashpw("Changed@999".encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

    try:
        db.login_attempts.delete_many({"identifier": {"$regex": f":{admin_email}$"}})
        db.users.update_one({"email": admin_email}, {"$set": {"password_hash": mutated_password_hash}})

        pre_seed_login = requests.post(
            f"{base_url}/api/auth/login",
            json={"email": admin_email, "password": admin_password},
            timeout=25,
        )
        assert pre_seed_login.status_code == 401

        if "/app/backend" not in sys.path:
            sys.path.append("/app/backend")
        import server as backend_server  # type: ignore

        asyncio.run(backend_server.seed_auth_and_indexes())
        db.login_attempts.delete_many({"identifier": {"$regex": f":{admin_email}$"}})

        post_seed_login = requests.post(
            f"{base_url}/api/auth/login",
            json={"email": admin_email, "password": admin_password},
            timeout=25,
        )
        assert post_seed_login.status_code == 200
    finally:
        mongo_client.close()
