from __future__ import annotations

import os
import uuid
from datetime import date, timedelta
from pathlib import Path

import pytest
import requests
from dotenv import dotenv_values
from pymongo import MongoClient


# Module: auth playbook core checks (bcrypt/hash, cookies, CORS credentials, brute-force lockout).
# Module: walker operational calendar contracts (capacity by period, day block toggle, slots).
# Module: scheduling/matching integration (availability impacts /walkers visibility and walk creation).

WALKER_CREDS = {"email": "walker@petpasso.com", "password": "Walker@123"}
CLIENT_CREDS = {"email": "cliente@petpasso.com", "password": "Cliente@123"}
ADMIN_CREDS = {"email": "admin@petpasso.com", "password": "Admin@123"}
TEST_TAG = "TEST_ITER77"


def _mongo_db():
    backend_env = dotenv_values("/app/backend/.env")
    mongo_url = os.environ.get("MONGO_URL") or backend_env.get("MONGO_URL")
    db_name = os.environ.get("DB_NAME") or backend_env.get("DB_NAME")
    if not mongo_url or not db_name:
        pytest.skip("MONGO_URL/DB_NAME não configurados")
    client = MongoClient(str(mongo_url).strip().strip('"'))
    return client, client[str(db_name).strip().strip('"')]


def _weekday_key(target_date: str) -> str:
    y, m, d = [int(part) for part in target_date.split("-")]
    # seg..dom
    map_keys = ["seg", "ter", "qua", "qui", "sex", "sab", "dom"]
    return map_keys[date(y, m, d).weekday()]


def _target_date(delta_days: int = 2) -> str:
    return (date.today() + timedelta(days=delta_days)).strftime("%Y-%m-%d")


def _login(base_url: str, email: str, password: str) -> requests.Session:
    session = requests.Session()
    response = session.post(
        f"{base_url}/api/auth/login",
        json={"email": email, "password": password},
        timeout=30,
    )
    assert response.status_code == 200, f"Login falhou ({email}): {response.status_code} {response.text}"
    payload = response.json() if response.text else {}
    token = payload.get("access_token")
    assert token, f"Token ausente no login de {email}"
    session.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/json"})
    return session


def _ensure_client_pet(client: requests.Session, base_url: str) -> tuple[dict, str | None]:
    pets_resp = client.get(f"{base_url}/api/pets", timeout=30)
    assert pets_resp.status_code == 200, pets_resp.text
    pets = pets_resp.json() if pets_resp.text else []
    if pets:
        return pets[0], None

    suffix = uuid.uuid4().hex[:8]
    payload = {
        "pet_name": f"{TEST_TAG}_PET_{suffix}",
        "behavioral_notes": "TEST comportamento",
        "photo_url": "",
        "owner_name": "TEST Cliente",
        "gets_along_with_dogs": True,
        "accepts_shared_walk": True,
        "pet_size": "Médio",
        "energy_level": "Médio",
        "pulls_leash": False,
        "dog_behavior": "Neutro",
    }
    created = client.post(f"{base_url}/api/pets", json=payload, timeout=30)
    assert created.status_code == 201, created.text
    pet = created.json()
    return pet, str(pet.get("id"))


@pytest.fixture
def calendar_env(base_url: str):
    walker = _login(base_url, WALKER_CREDS["email"], WALKER_CREDS["password"])
    client = _login(base_url, CLIENT_CREDS["email"], CLIENT_CREDS["password"])

    walker_me = walker.get(f"{base_url}/api/auth/me", timeout=30)
    assert walker_me.status_code == 200, walker_me.text
    walker_user_id = str((walker_me.json() or {}).get("id") or "")
    assert walker_user_id
    walker_public_id = f"partner-{walker_user_id}"

    settings_resp = walker.get(f"{base_url}/api/walker/availability-settings", timeout=30)
    assert settings_resp.status_code == 200, settings_resp.text
    original_settings = settings_resp.json()

    created_block_ids: list[str] = []
    created_pet_id = None
    try:
        yield {
            "walker": walker,
            "client": client,
            "walker_public_id": walker_public_id,
            "original_settings": original_settings,
            "created_block_ids": created_block_ids,
            "created_pet_id": created_pet_id,
        }
    finally:
        # Cleanup test-created blocks
        for block_id in created_block_ids:
            walker.delete(f"{base_url}/api/walker/availability-blocks/{block_id}", timeout=30)

        # Restore original availability config
        restore_payload = {
            "availability_days": original_settings.get("availability_days", ["seg", "ter", "qua", "qui", "sex"]),
            "availability_start_time": original_settings.get("availability_start_time", "08:00"),
            "availability_end_time": original_settings.get("availability_end_time", "18:00"),
            "availability_periods": original_settings.get("availability_periods", {}),
            "availability_capacity_by_period": original_settings.get(
                "availability_capacity_by_period", {"manha": 3, "tarde": 3, "noite": 2}
            ),
        }
        walker.patch(f"{base_url}/api/walker/availability", json=restore_payload, timeout=30)

        if created_pet_id:
            client.delete(f"{base_url}/api/pets/{created_pet_id}", timeout=30)

        walker.close()
        client.close()


def test_auth_playbook_core_controls(base_url: str):
    # bcrypt hash starts with $2b$
    mongo_client, db = _mongo_db()
    try:
        admin_row = db.users.find_one({"email": ADMIN_CREDS["email"]}, {"_id": 0, "password_hash": 1})
        assert admin_row is not None
        assert str(admin_row.get("password_hash") or "").startswith("$2b$")
    finally:
        mongo_client.close()

    # login sets httpOnly cookies
    login = requests.post(
        f"{base_url}/api/auth/login",
        json={"email": ADMIN_CREDS["email"], "password": ADMIN_CREDS["password"]},
        timeout=30,
    )
    assert login.status_code == 200, login.text
    set_cookie = "\n".join(login.raw.headers.get_all("Set-Cookie") if login.raw and login.raw.headers else [login.headers.get("set-cookie", "")])
    assert "access_token=" in set_cookie
    assert "refresh_token=" in set_cookie
    assert "HttpOnly" in set_cookie

    # CORS allows explicit origin + credentials
    origin = base_url.rstrip("/")
    preflight = requests.options(
        f"{base_url}/api/auth/login",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
        timeout=30,
    )
    assert preflight.status_code in {200, 204}
    assert preflight.headers.get("access-control-allow-credentials") == "true"
    assert preflight.headers.get("access-control-allow-origin") == origin

    # brute force lockout after 5 fails
    unique_ip = f"198.51.100.{int(uuid.uuid4().hex[:2], 16)}"
    statuses = []
    for _ in range(6):
        resp = requests.post(
            f"{base_url}/api/auth/login",
            json={"email": ADMIN_CREDS["email"], "password": "SenhaErrada!123"},
            headers={"x-forwarded-for": unique_ip},
            timeout=30,
        )
        statuses.append(resp.status_code)
    assert statuses[:5] == [401, 401, 401, 401, 401]
    assert statuses[5] == 429


def test_walker_availability_settings_returns_period_capacity_structure(base_url: str, calendar_env):
    walker = calendar_env["walker"]
    settings_resp = walker.get(f"{base_url}/api/walker/availability-settings", timeout=30)
    assert settings_resp.status_code == 200, settings_resp.text
    settings = settings_resp.json()

    caps = settings.get("availability_capacity_by_period") or {}
    assert set(caps.keys()) == {"manha", "tarde", "noite"}
    assert isinstance(caps["manha"], int)
    assert isinstance(caps["tarde"], int)
    assert isinstance(caps["noite"], int)


def test_capacity_zero_removes_slots_and_affects_client_scheduling_visibility(base_url: str, calendar_env):
    walker = calendar_env["walker"]
    client = calendar_env["client"]
    walker_public_id = calendar_env["walker_public_id"]
    original_settings = calendar_env["original_settings"]

    target_date = _target_date(2)
    weekday = _weekday_key(target_date)
    patch_payload = {
        "availability_days": [weekday],
        "availability_start_time": original_settings.get("availability_start_time", "08:00"),
        "availability_end_time": original_settings.get("availability_end_time", "18:00"),
        "availability_periods": original_settings.get("availability_periods", {}),
        "availability_capacity_by_period": {"manha": 0, "tarde": 0, "noite": 0},
    }
    patched = walker.patch(f"{base_url}/api/walker/availability", json=patch_payload, timeout=30)
    assert patched.status_code == 200, patched.text

    slots_resp = client.get(
        f"{base_url}/api/walkers/{walker_public_id}/availability-slots",
        params={"date": target_date, "duration_minutes": 30},
        timeout=30,
    )
    assert slots_resp.status_code == 200, slots_resp.text
    assert slots_resp.json().get("available_slots") == []

    walkers_resp = client.get(
        f"{base_url}/api/walkers",
        params={"date": target_date, "duration_minutes": 30, "preferred_time": "09:00", "tipo_passeio": "padrao"},
        timeout=30,
    )
    assert walkers_resp.status_code == 200, walkers_resp.text
    rows = walkers_resp.json()
    ids = {str(row.get("id") or "") for row in rows}
    assert walker_public_id not in ids


def test_capacity_restore_reenables_slots_and_allows_walk_creation(base_url: str, calendar_env):
    walker = calendar_env["walker"]
    client = calendar_env["client"]
    walker_public_id = calendar_env["walker_public_id"]
    original_settings = calendar_env["original_settings"]

    target_date = _target_date(2)
    weekday = _weekday_key(target_date)

    patch_payload = {
        "availability_days": [weekday],
        "availability_start_time": original_settings.get("availability_start_time", "08:00"),
        "availability_end_time": original_settings.get("availability_end_time", "18:00"),
        "availability_periods": original_settings.get("availability_periods", {}),
        "availability_capacity_by_period": {"manha": 3, "tarde": 3, "noite": 2},
    }
    patched = walker.patch(f"{base_url}/api/walker/availability", json=patch_payload, timeout=30)
    assert patched.status_code == 200, patched.text

    slots_resp = client.get(
        f"{base_url}/api/walkers/{walker_public_id}/availability-slots",
        params={"date": target_date, "duration_minutes": 30},
        timeout=30,
    )
    assert slots_resp.status_code == 200, slots_resp.text
    slots = slots_resp.json().get("available_slots") or []
    assert len(slots) > 0
    chosen_time = slots[0]

    pet, created_pet_id = _ensure_client_pet(client, base_url)
    if created_pet_id:
        calendar_env["created_pet_id"] = created_pet_id

    payload = {
        "pet_name": pet["pet_name"],
        "pet_id": pet["id"],
        "client_name": "Cliente Demo",
        "walk_date": target_date,
        "walk_time": chosen_time,
        "duration_minutes": 30,
        "walk_type": "Individual",
        "tipo_passeio": "padrao",
        "modo_inicio_passeio": "endereco_tutor",
        "walker_id": walker_public_id,
        "pickup_street": "Rua TEST",
        "pickup_number": "123",
        "pickup_neighborhood": "Pituba",
        "pickup_complement": "",
        "location_reference": "TEST",
        "pet_behavior_notes": "TEST",
        "notes": f"{TEST_TAG}_CREATE_AFTER_RESTORE",
    }
    created_walk = client.post(f"{base_url}/api/walks", json=payload, timeout=30)
    assert created_walk.status_code == 201, created_walk.text
    body = created_walk.json()
    assert body.get("walker_id") == walker_public_id
    assert body.get("walk_time") == chosen_time


def test_full_day_block_toggle_updates_day_unavailability(base_url: str, calendar_env):
    walker = calendar_env["walker"]
    client = calendar_env["client"]
    walker_public_id = calendar_env["walker_public_id"]
    created_block_ids = calendar_env["created_block_ids"]

    target_date = _target_date(3)
    block = walker.post(
        f"{base_url}/api/walker/availability-blocks",
        json={
            "start_date": target_date,
            "end_date": target_date,
            "start_time": "00:00",
            "end_time": "23:59",
            "full_day": True,
            "reason": f"{TEST_TAG}_FULL_DAY_BLOCK",
        },
        timeout=30,
    )
    assert block.status_code == 200, block.text
    payload = block.json()
    full_day_block = next(
        (b for b in (payload.get("blocks") or []) if b.get("reason") == f"{TEST_TAG}_FULL_DAY_BLOCK"),
        None,
    )
    assert full_day_block is not None
    created_block_ids.append(str(full_day_block["id"]))

    slots_blocked = client.get(
        f"{base_url}/api/walkers/{walker_public_id}/availability-slots",
        params={"date": target_date, "duration_minutes": 30},
        timeout=30,
    )
    assert slots_blocked.status_code == 200, slots_blocked.text
    assert slots_blocked.json().get("available_slots") == []

    delete_resp = walker.delete(f"{base_url}/api/walker/availability-blocks/{full_day_block['id']}", timeout=30)
    assert delete_resp.status_code == 200, delete_resp.text
    created_block_ids.remove(str(full_day_block["id"]))

    slots_reopened = client.get(
        f"{base_url}/api/walkers/{walker_public_id}/availability-slots",
        params={"date": target_date, "duration_minutes": 30},
        timeout=30,
    )
    assert slots_reopened.status_code == 200, slots_reopened.text
    assert isinstance(slots_reopened.json().get("available_slots"), list)


def test_scheduling_rejects_walk_when_capacity_zero(base_url: str, calendar_env):
    walker = calendar_env["walker"]
    client = calendar_env["client"]
    walker_public_id = calendar_env["walker_public_id"]
    original_settings = calendar_env["original_settings"]

    target_date = _target_date(4)
    weekday = _weekday_key(target_date)
    patch_payload = {
        "availability_days": [weekday],
        "availability_start_time": original_settings.get("availability_start_time", "08:00"),
        "availability_end_time": original_settings.get("availability_end_time", "18:00"),
        "availability_periods": original_settings.get("availability_periods", {}),
        "availability_capacity_by_period": {"manha": 0, "tarde": 0, "noite": 0},
    }
    patched = walker.patch(f"{base_url}/api/walker/availability", json=patch_payload, timeout=30)
    assert patched.status_code == 200, patched.text

    pet, created_pet_id = _ensure_client_pet(client, base_url)
    if created_pet_id:
        calendar_env["created_pet_id"] = created_pet_id

    payload = {
        "pet_name": pet["pet_name"],
        "pet_id": pet["id"],
        "client_name": "Cliente Demo",
        "walk_date": target_date,
        "walk_time": "09:00",
        "duration_minutes": 30,
        "walk_type": "Individual",
        "tipo_passeio": "padrao",
        "modo_inicio_passeio": "endereco_tutor",
        "walker_id": walker_public_id,
        "pickup_street": "Rua TEST",
        "pickup_number": "123",
        "pickup_neighborhood": "Pituba",
        "pickup_complement": "",
        "location_reference": "TEST",
        "pet_behavior_notes": "TEST",
        "notes": f"{TEST_TAG}_EXPECT_400_CAPACITY_ZERO",
    }
    created_walk = client.post(f"{base_url}/api/walks", json=payload, timeout=30)
    assert created_walk.status_code == 400, created_walk.text
    assert "dispon" in created_walk.text.lower() or "hor" in created_walk.text.lower()
