import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import requests
from dotenv import dotenv_values
from pymongo import MongoClient


# Module coverage: auth login and role routing, private API authorization, isolation, and cancellation penalty
def _credentials():
    return {
        "admin": {"email": "admin@petpasso.com", "password": "Admin@123"},
        "cliente": {"email": "cliente@petpasso.com", "password": "Cliente@123"},
        "passeador": {"email": "passeador@petpasso.com", "password": "Passeador@123"},
    }


def _auth_session(base_url: str, role: str) -> tuple[requests.Session, dict]:
    creds = _credentials()[role]
    session = requests.Session()
    session.headers.update({"Accept": "application/json", "Content-Type": "application/json"})
    login = session.post(f"{base_url}/api/auth/login", json=creds, timeout=20)
    assert login.status_code == 200, f"Login failed for role {role}: {login.text}"
    payload = login.json()
    token = payload.get("access_token")
    assert token, f"No access token for role {role}"
    session.headers.update({"Authorization": f"Bearer {token}"})
    return session, payload


def _now_plus(hours: int) -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=hours)


def _walk_payload(dt: datetime, pet_name: str = "TEST_walk_pet", client_name: str = "TEST_walk_client"):
    return {
        "pet_name": pet_name,
        "client_name": client_name,
        "walk_date": dt.strftime("%Y-%m-%d"),
        "walk_time": dt.strftime("%H:%M"),
        "duration_minutes": 30,
        "walk_type": "Individual",
        "walker_id": "walker-1",
        "pickup_street": "Rua Teste",
        "pickup_number": "10",
        "pickup_neighborhood": "Centro",
        "pickup_complement": "",
        "location_reference": "Praça",
        "pet_behavior_notes": "",
        "notes": "TEST flow",
    }


def _pet_payload(name: str):
    return {
        "pet_name": name,
        "behavioral_notes": "TEST behavior",
        "photo_url": "",
        "owner_name": "",
        "gets_along_with_dogs": True,
        "accepts_shared_walk": True,
        "pet_size": "Médio",
        "energy_level": "Médio",
        "pulls_leash": False,
        "dog_behavior": "Neutro",
    }


def _mongo_client_and_db():
    backend_env = dotenv_values("/app/backend/.env")
    mongo_url = os.environ.get("MONGO_URL") or backend_env.get("MONGO_URL")
    db_name = os.environ.get("DB_NAME") or backend_env.get("DB_NAME")
    if not mongo_url or not db_name:
        pytest.skip("MONGO_URL/DB_NAME not configured")
    return MongoClient(str(mongo_url).strip().strip('"')), str(db_name).strip().strip('"')


def test_login_cliente_passeador_admin_and_role_routing(base_url):
    for role in ["admin", "cliente", "passeador"]:
        session, _ = _auth_session(base_url, role)
        me = session.get(f"{base_url}/api/auth/me", timeout=20)
        assert me.status_code == 200
        me_data = me.json()
        assert me_data["role"] == role
        if role == "admin":
            assert me_data["isAdmin"] is True
        else:
            assert me_data["isAdmin"] is False
        session.close()


def test_private_apis_require_authorization_header(base_url):
    anon = requests.Session()
    walks = anon.get(f"{base_url}/api/walks", timeout=20)
    pets = anon.get(f"{base_url}/api/pets", timeout=20)
    owner = anon.get(f"{base_url}/api/owner-profile", timeout=20)
    assert walks.status_code == 401
    assert pets.status_code == 401
    assert owner.status_code == 401
    anon.close()


def test_pets_crud_and_delete_persistence_for_cliente(base_url):
    cliente, _ = _auth_session(base_url, "cliente")
    create = cliente.post(f"{base_url}/api/pets", json=_pet_payload("TEST_pet_crud_cliente"), timeout=20)
    assert create.status_code == 201
    pet_id = create.json()["id"]

    listed = cliente.get(f"{base_url}/api/pets", timeout=20)
    assert listed.status_code == 200
    assert any(item["id"] == pet_id for item in listed.json())

    delete = cliente.delete(f"{base_url}/api/pets/{pet_id}", timeout=20)
    assert delete.status_code == 204

    listed_after = cliente.get(f"{base_url}/api/pets", timeout=20)
    assert listed_after.status_code == 200
    assert all(item["id"] != pet_id for item in listed_after.json())
    cliente.close()


def test_owner_profile_isolation_between_cliente_and_admin(base_url):
    cliente, _ = _auth_session(base_url, "cliente")
    admin, _ = _auth_session(base_url, "admin")

    owner_payload = {
        "full_name": "TEST_OWNER_ISOLATION_CLIENTE",
        "phone": "71999990077",
        "email": "cliente_isolation@test.com",
        "street": "Rua Cliente",
        "number": "101",
        "neighborhood": "Bairro Cliente",
        "complement": "",
    }
    upsert = cliente.put(f"{base_url}/api/owner-profile", json=owner_payload, timeout=20)
    assert upsert.status_code == 200

    cliente_get = cliente.get(f"{base_url}/api/owner-profile", timeout=20)
    admin_get = admin.get(f"{base_url}/api/owner-profile", timeout=20)
    assert cliente_get.status_code == 200
    assert admin_get.status_code == 200
    assert cliente_get.json()["full_name"] == "TEST_OWNER_ISOLATION_CLIENTE"
    admin_payload = admin_get.json()
    if admin_payload:
        assert admin_payload.get("full_name") != "TEST_OWNER_ISOLATION_CLIENTE"

    cliente.close()
    admin.close()


def test_authenticated_schedule_with_pet_selection_and_walk_visibility(base_url):
    cliente, _ = _auth_session(base_url, "cliente")
    passeador, _ = _auth_session(base_url, "passeador")
    admin, _ = _auth_session(base_url, "admin")

    created_pet = cliente.post(f"{base_url}/api/pets", json=_pet_payload("TEST_pet_for_walk_visibility"), timeout=20)
    assert created_pet.status_code == 201
    pet_id = created_pet.json()["id"]

    schedule_payload = _walk_payload(_now_plus(30), pet_name="TEST_pet_for_walk_visibility")
    schedule_payload["pet_id"] = pet_id
    create_walk = cliente.post(f"{base_url}/api/walks", json=schedule_payload, timeout=20)
    assert create_walk.status_code == 201
    walk_id = create_walk.json()["id"]

    cliente_walk = cliente.get(f"{base_url}/api/walks/{walk_id}", timeout=20)
    assert cliente_walk.status_code == 200
    assert cliente_walk.json()["id"] == walk_id

    passeador_walks = passeador.get(f"{base_url}/api/walks", timeout=20)
    assert passeador_walks.status_code == 200

    admin_walk = admin.get(f"{base_url}/api/walks/{walk_id}", timeout=20)
    assert admin_walk.status_code == 200
    assert admin_walk.json()["id"] == walk_id

    cliente.close()
    passeador.close()
    admin.close()


def test_cancel_under_24h_returns_penalty_50_percent(base_url):
    cliente, _ = _auth_session(base_url, "cliente")
    created_pet = cliente.post(f"{base_url}/api/pets", json=_pet_payload("TEST_pet_cancel_24h"), timeout=20)
    assert created_pet.status_code == 201

    payload = _walk_payload(_now_plus(2), pet_name="TEST_pet_cancel_24h")
    create_walk = cliente.post(f"{base_url}/api/walks", json=payload, timeout=20)
    assert create_walk.status_code == 201
    walk_id = create_walk.json()["id"]

    cancel = cliente.patch(
        f"{base_url}/api/walks/{walk_id}/cancel",
        json={"tipoCancelamento": "cliente", "motivoCancelamento": "TEST cancel <24h"},
        timeout=20,
    )
    assert cancel.status_code == 200
    cancel_json = cancel.json()
    assert cancel_json["status"] == "Cancelado"
    assert cancel_json["penalidadePercentual"] == 50
    cliente.close()


def test_bcrypt_hash_prefix_for_seeded_admin_is_2b():
    client, db_name = _mongo_client_and_db()
    try:
        admin = client[db_name].users.find_one({"email": "admin@petpasso.com"}, {"password_hash": 1, "_id": 0})
        assert admin is not None
        assert str(admin.get("password_hash", "")).startswith("$2b$")
    finally:
        client.close()


def test_auth_cookie_and_cors_policy_expectations(base_url):
    # Playbook expectation check: login should set HttpOnly auth cookies
    login = requests.post(
        f"{base_url}/api/auth/login",
        json=_credentials()["admin"],
        timeout=20,
    )
    assert login.status_code == 200
    set_cookie = login.headers.get("set-cookie", "")
    assert "HttpOnly" in set_cookie and "access_token" in set_cookie and "refresh_token" in set_cookie

    # CORS preflight (observação: em alguns ambientes o edge/proxy pode sobrescrever headers)
    options = requests.options(
        f"{base_url}/api/auth/login",
        headers={
            "Origin": "https://petpasso-mvp.preview.emergentagent.com",
            "Access-Control-Request-Method": "POST",
        },
        timeout=20,
    )
    assert options.status_code in (200, 204)
    assert options.headers.get("access-control-allow-origin") is not None


def test_bruteforce_lockout_after_5_failures(base_url):
    session = requests.Session()
    isolated_email = f"test_lockout_{uuid.uuid4().hex[:8]}@petpasso.com"
    wrong_payload = {"email": isolated_email, "password": "wrong-pass"}

    for _ in range(5):
        bad = session.post(f"{base_url}/api/auth/login", json=wrong_payload, timeout=20)
        assert bad.status_code in (401, 429)

    final = session.post(f"{base_url}/api/auth/login", json=wrong_payload, timeout=20)
    assert final.status_code == 429
    session.close()