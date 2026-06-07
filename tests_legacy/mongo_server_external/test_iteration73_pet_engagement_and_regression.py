from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests
from dotenv import dotenv_values
from pymongo import MongoClient


# Module coverage: auth playbook contracts (cookies, lockout, CORS, bcrypt seed health).
# Module coverage: pet engagement contracts (highlights, badges, positive praise flow).
# Module coverage: regression contracts (schedule->confirm and admin cockpit access).

EXPECTED_BADGES = {"Pet ativo", "Cliente frequente", "Pet destaque"}


def _login(base_url: str, email: str, password: str) -> requests.Session:
    session = requests.Session()
    response = session.post(
        f"{base_url}/api/auth/login",
        json={"email": email, "password": password},
        timeout=30,
    )
    if response.status_code != 200:
        session.close()
        raise AssertionError(f"Login falhou para {email}: {response.status_code} {response.text}")
    data = response.json() or {}
    token = data.get("access_token")
    assert token, "access_token ausente no login"
    session.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/json"})
    return session


def _mongo_db():
    backend_env = Path("/app/backend/.env")
    env_values = dotenv_values(backend_env) if backend_env.exists() else {}
    mongo_url = os.environ.get("MONGO_URL") or env_values.get("MONGO_URL")
    db_name = os.environ.get("DB_NAME") or env_values.get("DB_NAME")
    if not mongo_url or not db_name:
        return None, None
    client = MongoClient(str(mongo_url).strip().strip('"'))
    return client, client[str(db_name).strip().strip('"')]


def _tomorrow_iso() -> str:
    return (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")


def _create_test_pet(client: requests.Session, base_url: str, suffix: str) -> dict:
    payload = {
        "pet_name": f"TEST_ENG_{suffix}",
        "behavioral_notes": "TEST comportamento amistoso",
        "photo_url": "",
        "owner_name": "TEST Cliente",
        "gets_along_with_dogs": True,
        "accepts_shared_walk": True,
        "pet_size": "Médio",
        "energy_level": "Médio",
        "pulls_leash": False,
        "dog_behavior": "Neutro",
    }
    response = client.post(f"{base_url}/api/pets", json=payload, timeout=30)
    assert response.status_code == 201, response.text
    pet = response.json()
    assert pet.get("id")
    return pet


def _pick_walker_id(client: requests.Session, base_url: str, preferred_name: str | None = None) -> str:
    response = client.get(
        f"{base_url}/api/walkers",
        params={
            "date": _tomorrow_iso(),
            "duration_minutes": 30,
            "preferred_time": "09:00",
            "tipo_passeio": "padrao",
        },
        timeout=30,
    )
    assert response.status_code == 200, response.text
    walkers = response.json()
    assert isinstance(walkers, list) and walkers, "Nenhum passeador retornado"
    target = walkers[0]
    if preferred_name:
        normalized_preferred = preferred_name.strip().lower()
        matched = next(
            (
                item
                for item in walkers
                if str(item.get("name") or "").strip().lower() == normalized_preferred
            ),
            None,
        )
        if matched:
            target = matched

    walker_id = str(target.get("id") or "").strip()
    assert walker_id, "walker_id ausente em /walkers"
    return walker_id


def _create_scheduled_walk(client: requests.Session, base_url: str, pet: dict, walker_id: str, notes: str) -> dict:
    payload = {
        "pet_name": pet["pet_name"],
        "pet_id": pet["id"],
        "client_name": "Cliente Demo",
        "walk_date": _tomorrow_iso(),
        "walk_time": "09:00",
        "duration_minutes": 30,
        "walk_type": "Individual",
        "tipo_passeio": "padrao",
        "modo_inicio_passeio": "endereco_tutor",
        "walker_id": walker_id,
        "pickup_street": "Rua TEST",
        "pickup_number": "100",
        "pickup_neighborhood": "Pituba",
        "pickup_complement": "Apto TEST",
        "location_reference": "Perto da praça",
        "pet_behavior_notes": "TEST comportado",
        "notes": notes,
    }
    response = client.post(f"{base_url}/api/walks", json=payload, timeout=30)
    assert response.status_code == 201, response.text
    walk = response.json()
    assert walk.get("id")
    return walk


def _login_walker_for_walk(base_url: str, walker_user_id: str) -> requests.Session:
    candidate_credentials = [
        ("walker@petpasso.com", "Walker@123"),
        ("passeador@petpasso.com", "Passeador@123"),
    ]
    for email, password in candidate_credentials:
        try:
            session = _login(base_url, email, password)
            me_response = session.get(f"{base_url}/api/auth/me", timeout=30)
            if me_response.status_code == 200 and str((me_response.json() or {}).get("id") or "") == walker_user_id:
                return session
            session.close()
        except AssertionError:
            continue
    pytest.skip("Nenhuma credencial de passeador corresponde ao walker_user_id do passeio criado")


def test_auth_login_sets_httponly_cookies(base_url: str):
    session = requests.Session()
    try:
        response = session.post(
            f"{base_url}/api/auth/login",
            json={"email": "cliente@petpasso.com", "password": "Cliente@123"},
            timeout=30,
        )
        assert response.status_code == 200, response.text
        set_cookie = response.headers.get("set-cookie", "")
        assert "access_token=" in set_cookie
        assert "refresh_token=" in set_cookie
        assert "HttpOnly" in set_cookie
    finally:
        session.close()


def test_auth_bruteforce_lockout_after_five_failures(base_url: str):
    session = requests.Session()
    email = f"lockout_test_{int(time.time())}@petpasso.com"
    try:
        for _ in range(5):
            response = session.post(
                f"{base_url}/api/auth/login",
                json={"email": email, "password": "senha-invalida"},
                timeout=30,
            )
            assert response.status_code == 401

        locked_response = session.post(
            f"{base_url}/api/auth/login",
            json={"email": email, "password": "senha-invalida"},
            timeout=30,
        )
        assert locked_response.status_code == 429, locked_response.text
    finally:
        mongo_client, database = _mongo_db()
        if mongo_client and database is not None:
            try:
                database.login_attempts.delete_many({"identifier": {"$regex": f":{email}$"}})
            finally:
                mongo_client.close()
        session.close()


def test_cors_preflight_allows_credentials_for_explicit_origin(base_url: str):
    response = requests.options(
        f"{base_url}/api/auth/login",
        headers={
            "Origin": "https://petpasso-mvp.preview.emergentagent.com",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
        timeout=30,
    )
    assert response.status_code in {200, 204}, response.text
    assert response.headers.get("access-control-allow-credentials") == "true"
    assert response.headers.get("access-control-allow-origin") == "https://petpasso-mvp.preview.emergentagent.com"


def test_seed_admin_hash_uses_bcrypt_2b_prefix_and_login_works(base_url: str):
    mongo_client, database = _mongo_db()
    if not mongo_client or database is None:
        return

    try:
        admin_user = database.users.find_one({"email": "admin@petpasso.com"}, {"_id": 0, "password_hash": 1})
        assert admin_user is not None
        password_hash = str(admin_user.get("password_hash") or "")
        assert password_hash.startswith("$2b$"), "password_hash do admin não está em formato bcrypt $2b$"
    finally:
        mongo_client.close()

    admin = _login(base_url, "admin@petpasso.com", "Admin@123")
    try:
        me = admin.get(f"{base_url}/api/auth/me", timeout=30)
        assert me.status_code == 200, me.text
        assert me.json().get("role") in {"admin", "super_admin"}
    finally:
        admin.close()


def test_client_pet_highlights_contract_and_badges(base_url: str):
    client = _login(base_url, "cliente@petpasso.com", "Cliente@123")
    try:
        response = client.get(f"{base_url}/api/pets/highlights", timeout=30)
        assert response.status_code == 200, response.text
        data = response.json()
        assert set(data.keys()) == {"pet_da_semana", "pet_do_mes", "pets_em_destaque"}
        assert isinstance(data.get("pets_em_destaque"), list)

        all_items = [data.get("pet_da_semana"), data.get("pet_do_mes")] + list(data.get("pets_em_destaque") or [])
        for item in [row for row in all_items if row]:
            badges = item.get("badges") or []
            assert isinstance(badges, list)
            assert set(badges).issubset(EXPECTED_BADGES)
    finally:
        client.close()


def test_schedule_confirm_regression_create_then_get_persists(base_url: str):
    client = _login(base_url, "cliente@petpasso.com", "Cliente@123")
    try:
        walker_id = _pick_walker_id(client, base_url, preferred_name="Carlos Oliveira")
        pet = _create_test_pet(client, base_url, "SCHEDULE")
        walk = _create_scheduled_walk(client, base_url, pet, walker_id, notes="TEST_SCHEDULE_CONFIRM_FLOW")

        get_response = client.get(f"{base_url}/api/walks/{walk['id']}", timeout=30)
        assert get_response.status_code == 200, get_response.text
        persisted = get_response.json()
        assert persisted.get("id") == walk["id"]
        assert persisted.get("status") == "Agendado"
        assert str(persisted.get("pet_name") or "").startswith("TEST_ENG_")
    finally:
        client.close()


def test_walker_positive_praise_flow_with_finished_history(base_url: str):
    client = _login(base_url, "cliente@petpasso.com", "Cliente@123")
    admin = _login(base_url, "admin@petpasso.com", "Admin@123")
    walker = None
    try:
        walker_id = _pick_walker_id(client, base_url, preferred_name="Carlos Oliveira")
        pet = _create_test_pet(client, base_url, "PRAISE")
        walk = _create_scheduled_walk(client, base_url, pet, walker_id, notes="TEST_PRAISE_FLOW")

        finish_response = admin.patch(
            f"{base_url}/api/admin/walks/{walk['id']}/status",
            json={"status": "Finalizado"},
            timeout=30,
        )
        assert finish_response.status_code == 200, finish_response.text

        finished_walk = admin.get(f"{base_url}/api/walks/{walk['id']}", timeout=30)
        assert finished_walk.status_code == 200, finished_walk.text
        walker_user_id = str((finished_walk.json() or {}).get("walker_user_id") or "")
        assert walker_user_id, "walker_user_id ausente no passeio finalizado"

        walker = _login_walker_for_walk(base_url, walker_user_id)

        praise_response = walker.post(
            f"{base_url}/api/pets/{pet['id']}/praise-tags",
            json={"walk_id": walk["id"], "tags": ["docil", "ativo"]},
            timeout=30,
        )
        assert praise_response.status_code == 201, praise_response.text
        praise = praise_response.json()
        assert praise.get("pet_id") == pet["id"]
        assert set(praise.get("tags") or []) == {"docil", "ativo"}
    finally:
        if walker is not None:
            walker.close()
        admin.close()
        client.close()


def test_admin_cockpit_dashboard_accessible(base_url: str):
    admin = _login(base_url, "admin@petpasso.com", "Admin@123")
    try:
        response = admin.get(f"{base_url}/api/admin/dashboard", timeout=30)
        assert response.status_code == 200, response.text
        payload = response.json()
        for required_key in [
            "total_clients",
            "total_active_walkers",
            "total_walks_finished",
            "total_walks_scheduled",
            "weekly_tips_amount",
        ]:
            assert required_key in payload
    finally:
        admin.close()
