from __future__ import annotations

import os
import uuid
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import bcrypt
import pytest
import requests
from dotenv import dotenv_values
from pymongo import MongoClient


# Module coverage: P0 admin occurrence resolution + premium override strict validation + auth playbook checks.


ADMIN_CREDS = {"email": "admin@petpasso.com", "password": "Admin@123"}
CLIENT_CREDS = {"email": "cliente@petpasso.com", "password": "Cliente@123"}


def _login_session(base_url: str, email: str, password: str) -> requests.Session:
    session = requests.Session()
    session.headers.update({"Accept": "application/json", "Content-Type": "application/json"})
    response = session.post(
        f"{base_url}/api/auth/login",
        json={"email": email, "password": password},
        timeout=30,
    )
    assert response.status_code == 200, response.text
    token = response.json().get("access_token")
    assert token
    session.headers.update({"Authorization": f"Bearer {token}"})
    return session


def _iso_date(days_ahead: int) -> str:
    return (date.today() + timedelta(days=days_ahead)).strftime("%Y-%m-%d")


def _resolve_walker_id(client_session: requests.Session, base_url: str) -> str:
    walkers_resp = client_session.get(f"{base_url}/api/walkers", timeout=30)
    assert walkers_resp.status_code == 200, walkers_resp.text
    walkers = walkers_resp.json()
    assert isinstance(walkers, list) and walkers
    preferred = [row for row in walkers if row.get("id") in {"walker-1", "partner-walker"}]
    return preferred[0]["id"] if preferred else walkers[0]["id"]


def _find_slot(client_session: requests.Session, base_url: str, walker_id: str) -> tuple[str, str]:
    for delta in range(2, 30):
        target_date = _iso_date(delta)
        response = client_session.get(
            f"{base_url}/api/walkers/{walker_id}/availability-slots",
            params={"date": target_date, "duration_minutes": 30},
            timeout=30,
        )
        if response.status_code != 200:
            continue
        slots = response.json().get("available_slots", [])
        if slots:
            return target_date, slots[0]
    pytest.skip("Sem slots disponíveis para criação de passeio de teste")


def _ensure_pet(client_session: requests.Session, base_url: str) -> tuple[dict[str, Any], str | None]:
    pets_resp = client_session.get(f"{base_url}/api/pets", timeout=30)
    assert pets_resp.status_code == 200, pets_resp.text
    pets = pets_resp.json()
    if pets:
        return pets[0], None

    suffix = uuid.uuid4().hex[:8]
    payload = {
        "pet_name": f"TEST_ITER27_PET_{suffix}",
        "behavioral_notes": "TEST_ITER27 behavioral notes",
        "photo_url": "",
        "owner_name": "TEST_ITER27 Owner",
        "gets_along_with_dogs": True,
        "accepts_shared_walk": True,
        "pet_size": "Médio",
        "energy_level": "Médio",
        "pulls_leash": False,
        "dog_behavior": "Neutro",
    }
    created = client_session.post(f"{base_url}/api/pets", json=payload, timeout=30)
    assert created.status_code == 201, created.text
    row = created.json()
    return row, row["id"]


def _create_walk_for_occurrence(client_session: requests.Session, base_url: str, pet: dict[str, Any], walker_id: str) -> str:
    walk_date, walk_time = _find_slot(client_session, base_url, walker_id)
    payload = {
        "pet_name": pet["pet_name"],
        "pet_id": pet["id"],
        "client_name": "TEST_ITER27 Cliente",
        "walk_date": walk_date,
        "walk_time": walk_time,
        "duration_minutes": 30,
        "walk_type": "Individual",
        "walker_id": walker_id,
        "pickup_street": "Rua TEST ITER27",
        "pickup_number": "27",
        "pickup_neighborhood": "Pituba",
        "pickup_complement": "",
        "location_reference": "TEST_ITER27",
        "pet_behavior_notes": "TEST_ITER27 behavior",
        "notes": f"TEST_ITER27_{uuid.uuid4().hex[:8]}",
    }
    created = client_session.post(f"{base_url}/api/walks", json=payload, timeout=30)
    assert created.status_code == 201, created.text
    walk_id = created.json()["id"]

    status_update = client_session.patch(
        f"{base_url}/api/walks/{walk_id}/status",
        json={"status": "Pendente de análise"},
        timeout=30,
    )
    assert status_update.status_code in {200, 400}, status_update.text
    return walk_id


@pytest.fixture
def sessions(base_url: str):
    admin = _login_session(base_url, ADMIN_CREDS["email"], ADMIN_CREDS["password"])
    client = _login_session(base_url, CLIENT_CREDS["email"], CLIENT_CREDS["password"])
    walker_id = _resolve_walker_id(client, base_url)
    pet, created_pet_id = _ensure_pet(client, base_url)

    yield {
        "admin": admin,
        "client": client,
        "walker_id": walker_id,
        "pet": pet,
        "created_pet_id": created_pet_id,
    }

    if created_pet_id:
        client.delete(f"{base_url}/api/pets/{created_pet_id}", timeout=20)
    admin.close()
    client.close()


def test_auth_login_sets_http_only_cookies(base_url: str):
    response = requests.post(
        f"{base_url}/api/auth/login",
        json=ADMIN_CREDS,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        timeout=30,
    )
    assert response.status_code == 200, response.text
    set_cookie = response.headers.get("set-cookie", "")
    assert "access_token=" in set_cookie
    assert "refresh_token=" in set_cookie
    assert "HttpOnly" in set_cookie


def test_auth_cors_preflight_allows_credentials_with_explicit_origin(base_url: str):
    preflight = requests.options(
        f"{base_url}/api/auth/login",
        headers={
            "Origin": base_url,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
        timeout=30,
    )
    assert preflight.status_code in {200, 204}
    assert preflight.headers.get("Access-Control-Allow-Credentials", "").lower() == "true"
    assert preflight.headers.get("Access-Control-Allow-Origin") not in {"", "*"}


def test_auth_lockout_after_five_failed_attempts(base_url: str):
    email = f"lockout_iter27_{uuid.uuid4().hex[:6]}@petpasso.com"
    payload = {"email": email, "password": "wrong-password"}
    statuses = []

    for _ in range(6):
        response = requests.post(f"{base_url}/api/auth/login", json=payload, timeout=30)
        statuses.append(response.status_code)

    assert statuses[:5] == [401, 401, 401, 401, 401]
    assert statuses[5] == 429


def test_seeded_admin_hash_bcrypt_and_password_matches_env():
    backend_env_path = Path("/app/backend/.env")
    values = dotenv_values(backend_env_path) if backend_env_path.exists() else {}

    mongo_url = (os.environ.get("MONGO_URL") or values.get("MONGO_URL") or "").strip().strip('"')
    db_name = (os.environ.get("DB_NAME") or values.get("DB_NAME") or "").strip().strip('"')
    admin_email = (os.environ.get("ADMIN_EMAIL") or values.get("ADMIN_EMAIL") or "").strip().lower()
    admin_password = (os.environ.get("ADMIN_PASSWORD") or values.get("ADMIN_PASSWORD") or "").strip()

    if not mongo_url or not db_name or not admin_email or not admin_password:
        pytest.skip("Configuração de banco/admin indisponível para validação de seed")

    mongo = MongoClient(mongo_url)
    try:
        user = mongo[db_name].users.find_one({"email": admin_email}, {"_id": 0, "password_hash": 1})
        assert user and isinstance(user.get("password_hash"), str)
        hash_value = user["password_hash"]
        assert hash_value.startswith("$2b$")
        assert bcrypt.checkpw(admin_password.encode("utf-8"), hash_value.encode("utf-8"))
    finally:
        mongo.close()


def test_occurrence_action_mark_resolved_requires_note_min_15(base_url: str, sessions: dict[str, Any]):
    admin = sessions["admin"]
    client = sessions["client"]
    walk_id = _create_walk_for_occurrence(client, base_url, sessions["pet"], sessions["walker_id"])

    too_short_note = "curta nota"
    response = admin.post(
        f"{base_url}/api/admin/occurrences/{walk_id}/action",
        json={"action": "mark_resolved", "note": too_short_note},
        timeout=30,
    )
    assert response.status_code == 400
    assert "mín. 15" in response.text


def test_occurrence_action_mark_resolved_accepts_note_min_15_and_persists(base_url: str, sessions: dict[str, Any]):
    admin = sessions["admin"]
    client = sessions["client"]
    walk_id = _create_walk_for_occurrence(client, base_url, sessions["pet"], sessions["walker_id"])

    valid_note = "Observação interna válida com quinze+"
    response = admin.post(
        f"{base_url}/api/admin/occurrences/{walk_id}/action",
        json={"action": "mark_resolved", "note": valid_note},
        timeout=30,
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["occurrence_status"] == "resolvido"
    assert payload["resolved"] is True
    assert payload["internal_note"] == valid_note

    check = admin.get(f"{base_url}/api/admin/occurrences", timeout=30)
    assert check.status_code == 200, check.text
    row = next(item for item in check.json() if item["id"] == walk_id)
    assert row["resolved"] is True
    assert row["internal_note"] == valid_note


def test_walker_force_premium_requires_justification_min_30(base_url: str, sessions: dict[str, Any]):
    admin = sessions["admin"]
    perf = admin.get(f"{base_url}/api/admin/walkers/performance", timeout=30)
    assert perf.status_code == 200, perf.text
    rows = perf.json()
    assert isinstance(rows, list) and rows
    walker_id = rows[0]["user_id"]

    short_note = "justificativa curta"
    response = admin.post(
        f"{base_url}/api/admin/walkers/{walker_id}/action",
        json={"action": "force_feature_premium", "note": short_note},
        timeout=30,
    )
    assert response.status_code == 400
    assert "mín. 30" in response.text


def test_walker_force_premium_accepts_justification_min_30_and_sets_override(base_url: str, sessions: dict[str, Any]):
    admin = sessions["admin"]
    perf = admin.get(f"{base_url}/api/admin/walkers/performance", timeout=30)
    assert perf.status_code == 200, perf.text
    rows = perf.json()
    assert isinstance(rows, list) and rows
    walker_id = rows[0]["user_id"]

    valid_note = "Override autorizado por contexto operacional e histórico comprovado recente."
    response = admin.post(
        f"{base_url}/api/admin/walkers/{walker_id}/action",
        json={"action": "force_feature_premium", "note": valid_note},
        timeout=30,
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["premium_override"] is True

    persisted = admin.get(f"{base_url}/api/admin/walkers/performance", timeout=30)
    assert persisted.status_code == 200, persisted.text
    updated_row = next(item for item in persisted.json() if item["user_id"] == walker_id)
    assert updated_row["premium_override"] is True
    assert updated_row["is_premium_featured"] is True


def test_walker_feature_premium_strict_blocks_ineligible_without_override(base_url: str, sessions: dict[str, Any]):
    admin = sessions["admin"]
    perf = admin.get(f"{base_url}/api/admin/walkers/performance", timeout=30)
    assert perf.status_code == 200, perf.text
    rows = perf.json()
    assert isinstance(rows, list) and rows

    ineligible = next((row for row in rows if not row.get("can_be_featured_by_rule")), None)
    if not ineligible:
        pytest.skip("Nenhum passeador inelegível disponível para validação de bloqueio estrito")

    response = admin.post(
        f"{base_url}/api/admin/walkers/{ineligible['user_id']}/action",
        json={"action": "feature_premium", "note": "Tentativa sem override"},
        timeout=30,
    )
    assert response.status_code == 400
    assert "não atende critérios" in response.text
