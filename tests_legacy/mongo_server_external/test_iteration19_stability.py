import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import requests
from dotenv import dotenv_values
from pymongo import MongoClient


# Module coverage: auth hardening playbook + critical scheduling/admin/walker API stability
CLIENTE_CREDS = {"email": "cliente@petpasso.com", "password": "Cliente@123"}
ADMIN_CREDS = {"email": "admin@petpasso.com", "password": "Admin@123"}
WALKER_CREDS = {"email": "walker@petpasso.com", "password": "Walker@123"}


def _login_session(base_url: str, creds: dict) -> requests.Session:
    session = requests.Session()
    session.headers.update({"Accept": "application/json", "Content-Type": "application/json"})
    response = session.post(f"{base_url}/api/auth/login", json=creds, timeout=20)
    assert response.status_code == 200, response.text
    token = response.json().get("access_token")
    assert token
    session.headers.update({"Authorization": f"Bearer {token}"})
    return session


def _mongo_client_and_db_name() -> tuple[MongoClient, str]:
    backend_env = dotenv_values("/app/backend/.env")
    mongo_url = os.environ.get("MONGO_URL") or backend_env.get("MONGO_URL")
    db_name = os.environ.get("DB_NAME") or backend_env.get("DB_NAME")
    if not mongo_url or not db_name:
        pytest.skip("MONGO_URL/DB_NAME not configured")
    client = MongoClient(str(mongo_url).strip().strip('"'))
    return client, str(db_name).strip().strip('"')


def _create_cliente_pet(session: requests.Session, base_url: str, suffix: str) -> str:
    payload = {
        "pet_name": f"TEST_ITER19_PET_{suffix}",
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
    created = session.post(f"{base_url}/api/pets", json=payload, timeout=20)
    assert created.status_code == 201, created.text
    return created.json()["id"]


def test_login_cliente_admin_walker_success(base_url):
    for creds, expected_role in [
        (CLIENTE_CREDS, "cliente"),
        (ADMIN_CREDS, "admin"),
        (WALKER_CREDS, "passeador"),
    ]:
        session = _login_session(base_url, creds)
        me = session.get(f"{base_url}/api/auth/me", timeout=20)
        assert me.status_code == 200
        assert me.json().get("role") == expected_role
        session.close()


def test_login_sets_http_only_auth_cookies(base_url):
    response = requests.post(f"{base_url}/api/auth/login", json=ADMIN_CREDS, timeout=20)
    assert response.status_code == 200
    set_cookie = response.headers.get("set-cookie", "")
    assert "HttpOnly" in set_cookie
    assert "access_token" in set_cookie
    assert "refresh_token" in set_cookie


def test_cors_preflight_allows_explicit_origin_and_credentials(base_url):
    response = requests.options(
        f"{base_url}/api/auth/login",
        headers={
            "Origin": "https://petpasso-mvp.preview.emergentagent.com",
            "Access-Control-Request-Method": "POST",
        },
        timeout=20,
    )
    assert response.status_code in (200, 204)
    assert response.headers.get("access-control-allow-origin") is not None
    assert response.headers.get("access-control-allow-credentials") in ("true", "True", "TRUE")


def test_bruteforce_lockout_after_five_invalid_attempts(base_url):
    fake_email = f"test_lockout_iter19_{uuid.uuid4().hex[:8]}@petpasso.com"
    payload = {"email": fake_email, "password": "wrong-pass"}
    session = requests.Session()
    for _ in range(5):
        resp = session.post(f"{base_url}/api/auth/login", json=payload, timeout=20)
        assert resp.status_code in (401, 429)
    final = session.post(f"{base_url}/api/auth/login", json=payload, timeout=20)
    assert final.status_code == 429
    session.close()


def test_admin_dashboard_allowed_for_admin_and_blocked_for_walker(base_url):
    admin = _login_session(base_url, ADMIN_CREDS)
    walker = _login_session(base_url, WALKER_CREDS)

    admin_dash = admin.get(f"{base_url}/api/admin/dashboard", timeout=20)
    walker_dash = walker.get(f"{base_url}/api/admin/dashboard", timeout=20)

    assert admin_dash.status_code == 200
    assert walker_dash.status_code == 403
    admin.close()
    walker.close()


def test_schedule_premium_payload_persists_start_mode_and_destination(base_url):
    cliente = _login_session(base_url, CLIENTE_CREDS)
    pet_id = _create_cliente_pet(cliente, base_url, "PREMIUM")

    walkers_resp = cliente.get(f"{base_url}/api/walkers", timeout=20)
    assert walkers_resp.status_code == 200
    premium_candidates = [
        w
        for w in walkers_resp.json()
        if w.get("possuiVeiculo") and w.get("aceitaDeslocamentoPremium") and w.get("ativoParaTransportePremium")
    ]
    if not premium_candidates:
        pytest.skip("No premium eligible walker available in seed")

    payload = {
        "pet_name": "TEST_ITER19_PET_PREMIUM",
        "pet_id": pet_id,
        "client_name": "TEST_ITER19_CLIENT_PREMIUM",
        "walk_date": (datetime.now(timezone.utc) + timedelta(days=2)).strftime("%Y-%m-%d"),
        "walk_time": "10:30",
        "duration_minutes": 30,
        "walk_type": "Individual",
        "modo_inicio_passeio": "deslocamento_premium",
        "local_destino_nome": "Farol da Barra",
        "local_destino_referencia": "Barra",
        "walker_id": premium_candidates[0]["id"],
        "pickup_street": "Avenida Paulo VI",
        "pickup_number": "1900",
        "pickup_neighborhood": "Pituba",
        "pickup_complement": "",
        "location_reference": "Salvador",
        "pet_behavior_notes": "",
        "notes": "TEST_ITER19_PREMIUM_NOTES",
    }
    created = cliente.post(f"{base_url}/api/walks", json=payload, timeout=20)
    assert created.status_code == 201, created.text
    walk_id = created.json()["id"]

    fetched = cliente.get(f"{base_url}/api/walks/{walk_id}", timeout=20)
    assert fetched.status_code == 200
    data = fetched.json()
    assert data.get("modoInicioPasseio") == "deslocamento_premium"
    local_destino = data.get("localDestinoPasseio") or {}
    assert local_destino.get("nome") == "Farol da Barra"
    assert local_destino.get("referencia") == "Barra"
    cliente.close()


def test_schedule_rejects_premium_without_destination(base_url):
    cliente = _login_session(base_url, CLIENTE_CREDS)
    pet_id = _create_cliente_pet(cliente, base_url, "PREMIUM_FAIL")

    walkers_resp = cliente.get(f"{base_url}/api/walkers", timeout=20)
    assert walkers_resp.status_code == 200
    premium_candidates = [
        w
        for w in walkers_resp.json()
        if w.get("possuiVeiculo") and w.get("aceitaDeslocamentoPremium") and w.get("ativoParaTransportePremium")
    ]
    if not premium_candidates:
        pytest.skip("No premium eligible walker available in seed")

    payload = {
        "pet_name": "TEST_ITER19_PET_PREMIUM_FAIL",
        "pet_id": pet_id,
        "client_name": "TEST_ITER19_CLIENT_PREMIUM_FAIL",
        "walk_date": (datetime.now(timezone.utc) + timedelta(days=2)).strftime("%Y-%m-%d"),
        "walk_time": "11:00",
        "duration_minutes": 30,
        "walk_type": "Individual",
        "modo_inicio_passeio": "deslocamento_premium",
        "walker_id": premium_candidates[0]["id"],
        "pickup_street": "Avenida Paulo VI",
        "pickup_number": "1900",
        "pickup_neighborhood": "Pituba",
        "pickup_complement": "",
        "location_reference": "Salvador",
        "pet_behavior_notes": "",
        "notes": "TEST_ITER19_PREMIUM_FAIL_NOTES",
    }
    created = cliente.post(f"{base_url}/api/walks", json=payload, timeout=20)
    assert created.status_code == 400
    assert "destino" in created.json().get("detail", "").lower()
    cliente.close()


def test_seed_admin_hash_uses_bcrypt_2b_prefix(base_url):
    _ = base_url
    client, db_name = _mongo_client_and_db_name()
    try:
        admin = client[db_name].users.find_one({"email": "admin@petpasso.com"}, {"password_hash": 1})
        assert admin is not None
        assert str(admin.get("password_hash", "")).startswith("$2b$")
    finally:
        client.close()
