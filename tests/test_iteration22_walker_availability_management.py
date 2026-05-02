import uuid
from datetime import date, datetime, time, timedelta

import pytest
import requests


# Module coverage: walker availability management + block/unavailable conflict safety + client slot filtering


WALKER_CREDS = {"email": "walker@petpasso.com", "password": "Walker@123"}
CLIENT_CREDS = {"email": "cliente@petpasso.com", "password": "Cliente@123"}


def _login(base_url: str, email: str, password: str) -> requests.Session:
    session = requests.Session()
    session.headers.update({"Accept": "application/json", "Content-Type": "application/json"})
    response = session.post(f"{base_url}/api/auth/login", json={"email": email, "password": password}, timeout=30)
    assert response.status_code == 200, response.text

    data = response.json()
    token = data.get("access_token")
    assert token
    session.headers.update({"Authorization": f"Bearer {token}"})
    return session


def _to_clock(minutes_total: int) -> str:
    return f"{minutes_total // 60:02d}:{minutes_total % 60:02d}"


def _plus_minutes(clock_value: str, minutes: int) -> str:
    parsed = datetime.strptime(clock_value, "%H:%M")
    shifted = parsed + timedelta(minutes=minutes)
    return shifted.strftime("%H:%M")


def _iso_date(delta_days: int) -> str:
    return (date.today() + timedelta(days=delta_days)).strftime("%Y-%m-%d")


def _walker_public_id(client_session: requests.Session, base_url: str, walker_me: dict) -> str:
    walkers_resp = client_session.get(f"{base_url}/api/walkers", timeout=30)
    assert walkers_resp.status_code == 200, walkers_resp.text
    walkers = walkers_resp.json()

    full_name = walker_me.get("full_name", "")
    user_id = walker_me.get("id", "")
    slug = full_name.lower().replace(" ", "-")
    preferred_ids = {user_id, f"partner-{user_id}", f"partner-{slug}"}

    exact = [w for w in walkers if w.get("name") == full_name and w.get("id") in preferred_ids]
    if exact:
        return exact[0]["id"]

    by_name = [w for w in walkers if w.get("name") == full_name]
    if by_name:
        return by_name[0]["id"]

    pytest.skip("Passeador do teste não encontrado na lista pública /walkers")


def _find_date_with_available_slot(
    client_session: requests.Session,
    base_url: str,
    walker_id: str,
    duration_minutes: int,
    *,
    min_delta_days: int = 1,
    max_delta_days: int = 28,
) -> tuple[str, str, list[str]]:
    for delta in range(min_delta_days, max_delta_days + 1):
        target_date = _iso_date(delta)
        response = client_session.get(
            f"{base_url}/api/walkers/{walker_id}/availability-slots",
            params={"date": target_date, "duration_minutes": duration_minutes},
            timeout=30,
        )
        if response.status_code != 200:
            continue
        slots = response.json().get("available_slots", [])
        if slots:
            return target_date, slots[0], slots
    pytest.skip("Não foi encontrado slot disponível no intervalo de datas para o passeador")


def _ensure_client_pet(client_session: requests.Session, base_url: str) -> tuple[dict, bool]:
    pets_resp = client_session.get(f"{base_url}/api/pets", timeout=30)
    assert pets_resp.status_code == 200, pets_resp.text
    pets = pets_resp.json()
    if pets:
        return pets[0], False

    suffix = uuid.uuid4().hex[:8]
    payload = {
        "pet_name": f"TEST_ITER22_PET_{suffix}",
        "behavioral_notes": "TEST_ITER22 behavior",
        "photo_url": "",
        "owner_name": "TEST_ITER22 Owner",
        "gets_along_with_dogs": True,
        "accepts_shared_walk": True,
        "pet_size": "Médio",
        "energy_level": "Médio",
        "pulls_leash": False,
        "dog_behavior": "Neutro",
    }
    created = client_session.post(f"{base_url}/api/pets", json=payload, timeout=30)
    assert created.status_code == 201, created.text
    return created.json(), True


@pytest.fixture
def sessions(base_url: str):
    walker_session = _login(base_url, WALKER_CREDS["email"], WALKER_CREDS["password"])
    client_session = _login(base_url, CLIENT_CREDS["email"], CLIENT_CREDS["password"])

    walker_me_resp = walker_session.get(f"{base_url}/api/auth/me", timeout=30)
    assert walker_me_resp.status_code == 200, walker_me_resp.text
    walker_me = walker_me_resp.json()
    walker_id = _walker_public_id(client_session, base_url, walker_me)

    created_pet_id = None
    pet, created = _ensure_client_pet(client_session, base_url)
    if created:
        created_pet_id = pet["id"]

    created_block_ids: list[str] = []

    yield {
        "walker": walker_session,
        "client": client_session,
        "walker_me": walker_me,
        "walker_id": walker_id,
        "pet": pet,
        "created_block_ids": created_block_ids,
        "created_pet_id": created_pet_id,
    }

    # best-effort cleanup of test-only block IDs created in this run
    for block_id in created_block_ids:
        walker_session.delete(f"{base_url}/api/walker/availability-blocks/{block_id}", timeout=20)

    if created_pet_id:
        client_session.delete(f"{base_url}/api/pets/{created_pet_id}", timeout=20)

    walker_session.close()
    client_session.close()


def test_weekly_availability_patch_updates_days_and_time_window(base_url, sessions):
    walker = sessions["walker"]

    payload = {
        "availability_days": ["seg", "ter", "qua", "qui", "sex", "sab"],
        "availability_start_time": "09:00",
        "availability_end_time": "17:00",
    }
    update_resp = walker.patch(f"{base_url}/api/walker/availability", json=payload, timeout=30)
    assert update_resp.status_code == 200, update_resp.text

    updated_user = update_resp.json()
    assert updated_user["role"] == "passeador"
    assert updated_user["horarios_disponiveis"]["30"]["seg"][0] == "09:00"

    settings_resp = walker.get(f"{base_url}/api/walker/availability-settings", timeout=30)
    assert settings_resp.status_code == 200, settings_resp.text
    settings = settings_resp.json()
    assert settings["availability_days"] == payload["availability_days"]
    assert settings["availability_start_time"] == payload["availability_start_time"]
    assert settings["availability_end_time"] == payload["availability_end_time"]


def test_api_contract_post_walker_availability_expected_in_spec(base_url, sessions):
    walker = sessions["walker"]
    payload = {
        "availability_days": ["seg", "ter", "qua", "qui", "sex"],
        "availability_start_time": "08:00",
        "availability_end_time": "18:00",
    }
    response = walker.post(f"{base_url}/api/walker/availability", json=payload, timeout=30)
    assert response.status_code == 200, response.text


def test_create_block_full_day_and_interval_then_delete_block(base_url, sessions):
    walker = sessions["walker"]
    client = sessions["client"]
    walker_id = sessions["walker_id"]
    created_block_ids = sessions["created_block_ids"]

    target_date, first_slot, _ = _find_date_with_available_slot(client, base_url, walker_id, 30, min_delta_days=5)
    interval_end = _plus_minutes(first_slot, 30)

    interval_resp = walker.post(
        f"{base_url}/api/walker/availability-blocks",
        json={
            "start_date": target_date,
            "start_time": first_slot,
            "end_date": target_date,
            "end_time": interval_end,
            "full_day": False,
            "reason": "TEST_ITER22 interval block",
        },
        timeout=30,
    )
    assert interval_resp.status_code == 200, interval_resp.text
    interval_payload = interval_resp.json()
    assert isinstance(interval_payload["blocks"], list)
    assert any(block["reason"] == "TEST_ITER22 interval block" for block in interval_payload["blocks"])

    interval_block = next(block for block in interval_payload["blocks"] if block["reason"] == "TEST_ITER22 interval block")
    created_block_ids.append(interval_block["id"])
    assert interval_block["is_full_day"] is False
    assert interval_block["block_type"] == "manual"

    full_day_date = _iso_date(16)
    full_day_resp = walker.post(
        f"{base_url}/api/walker/availability-blocks",
        json={
            "start_date": full_day_date,
            "start_time": "00:00",
            "end_date": full_day_date,
            "end_time": "23:59",
            "full_day": True,
            "reason": "TEST_ITER22 full day block",
        },
        timeout=30,
    )
    assert full_day_resp.status_code == 200, full_day_resp.text
    full_day_payload = full_day_resp.json()
    full_day_block = next(block for block in full_day_payload["blocks"] if block["reason"] == "TEST_ITER22 full day block")
    created_block_ids.append(full_day_block["id"])
    assert full_day_block["is_full_day"] is True

    delete_resp = walker.delete(f"{base_url}/api/walker/availability-blocks/{full_day_block['id']}", timeout=30)
    assert delete_resp.status_code == 200, delete_resp.text
    after_delete = delete_resp.json()
    assert all(block["id"] != full_day_block["id"] for block in after_delete["blocks"])

    created_block_ids.remove(full_day_block["id"])


def test_unavailable_custom_period_apply_and_clear(base_url, sessions):
    walker = sessions["walker"]
    target_date = _iso_date(18)

    set_resp = walker.post(
        f"{base_url}/api/walker/unavailable",
        json={
            "mode": "custom_period",
            "start_date": target_date,
            "start_time": "10:00",
            "end_date": target_date,
            "end_time": "12:00",
            "reason": "TEST_ITER22 unavailable custom",
        },
        timeout=30,
    )
    assert set_resp.status_code == 200, set_resp.text
    set_payload = set_resp.json()
    assert set_payload["unavailable_until"] is not None
    assert any(block["block_type"] == "quick_unavailable" for block in set_payload["blocks"])

    clear_resp = walker.delete(f"{base_url}/api/walker/unavailable", timeout=30)
    assert clear_resp.status_code == 200, clear_resp.text
    cleared = clear_resp.json()
    assert cleared["unavailable_until"] is None
    assert all(block["block_type"] != "quick_unavailable" for block in cleared["blocks"])


def test_conflict_blocks_creation_when_walk_exists(base_url, sessions):
    walker = sessions["walker"]
    client = sessions["client"]
    walker_id = sessions["walker_id"]
    pet = sessions["pet"]

    target_date, slot, slots = _find_date_with_available_slot(client, base_url, walker_id, 30, min_delta_days=6)
    assert slot in slots

    create_walk_resp = client.post(
        f"{base_url}/api/walks",
        json={
            "pet_name": pet["pet_name"],
            "pet_id": pet["id"],
            "client_name": "TEST_ITER22 Client",
            "walk_date": target_date,
            "walk_time": slot,
            "duration_minutes": 30,
            "walk_type": "Individual",
            "walker_id": walker_id,
            "pickup_street": "Rua TEST_ITER22",
            "pickup_number": "22",
            "pickup_neighborhood": "Pituba",
            "pickup_complement": "",
            "location_reference": "TEST_ITER22",
            "pet_behavior_notes": "",
            "notes": f"TEST_ITER22_CONFLICT_{uuid.uuid4().hex[:8]}",
        },
        timeout=30,
    )
    assert create_walk_resp.status_code == 201, create_walk_resp.text

    block_resp = walker.post(
        f"{base_url}/api/walker/availability-blocks",
        json={
            "start_date": target_date,
            "start_time": slot,
            "end_date": target_date,
            "end_time": _plus_minutes(slot, 30),
            "full_day": False,
            "reason": "TEST_ITER22 should conflict",
        },
        timeout=30,
    )
    assert block_resp.status_code == 400
    assert "Existe passeio já agendado" in block_resp.json().get("detail", "")


def test_conflict_blocks_unavailable_when_walk_exists(base_url, sessions):
    walker = sessions["walker"]
    client = sessions["client"]
    walker_id = sessions["walker_id"]
    pet = sessions["pet"]

    target_date, slot, _ = _find_date_with_available_slot(client, base_url, walker_id, 30, min_delta_days=7)

    create_walk_resp = client.post(
        f"{base_url}/api/walks",
        json={
            "pet_name": pet["pet_name"],
            "pet_id": pet["id"],
            "client_name": "TEST_ITER22 Client",
            "walk_date": target_date,
            "walk_time": slot,
            "duration_minutes": 30,
            "walk_type": "Individual",
            "walker_id": walker_id,
            "pickup_street": "Rua TEST_ITER22",
            "pickup_number": "23",
            "pickup_neighborhood": "Pituba",
            "pickup_complement": "",
            "location_reference": "TEST_ITER22",
            "pet_behavior_notes": "",
            "notes": f"TEST_ITER22_UNAVAILABLE_CONFLICT_{uuid.uuid4().hex[:8]}",
        },
        timeout=30,
    )
    assert create_walk_resp.status_code == 201, create_walk_resp.text

    unavailable_resp = walker.post(
        f"{base_url}/api/walker/unavailable",
        json={
            "mode": "custom_period",
            "start_date": target_date,
            "start_time": slot,
            "end_date": target_date,
            "end_time": _plus_minutes(slot, 30),
            "reason": "TEST_ITER22 unavailable conflict",
        },
        timeout=30,
    )
    assert unavailable_resp.status_code == 400
    assert "Existe passeio já agendado" in unavailable_resp.json().get("detail", "")


def test_client_slots_hide_blocked_time_and_walk_creation_rejects_blocked_slot(base_url, sessions):
    walker = sessions["walker"]
    client = sessions["client"]
    walker_id = sessions["walker_id"]
    pet = sessions["pet"]
    created_block_ids = sessions["created_block_ids"]

    target_date, original_slot, original_slots = _find_date_with_available_slot(client, base_url, walker_id, 30, min_delta_days=10)
    assert original_slot in original_slots

    block_resp = walker.post(
        f"{base_url}/api/walker/availability-blocks",
        json={
            "start_date": target_date,
            "start_time": original_slot,
            "end_date": target_date,
            "end_time": _plus_minutes(original_slot, 30),
            "full_day": False,
            "reason": "TEST_ITER22 slot filtered",
        },
        timeout=30,
    )
    assert block_resp.status_code == 200, block_resp.text
    block_payload = block_resp.json()
    block = next(item for item in block_payload["blocks"] if item["reason"] == "TEST_ITER22 slot filtered")
    created_block_ids.append(block["id"])

    slots_after_resp = client.get(
        f"{base_url}/api/walkers/{walker_id}/availability-slots",
        params={"date": target_date, "duration_minutes": 30},
        timeout=30,
    )
    assert slots_after_resp.status_code == 200, slots_after_resp.text
    filtered_slots = slots_after_resp.json().get("available_slots", [])
    assert original_slot not in filtered_slots

    create_walk_resp = client.post(
        f"{base_url}/api/walks",
        json={
            "pet_name": pet["pet_name"],
            "pet_id": pet["id"],
            "client_name": "TEST_ITER22 Client",
            "walk_date": target_date,
            "walk_time": original_slot,
            "duration_minutes": 30,
            "walk_type": "Individual",
            "walker_id": walker_id,
            "pickup_street": "Rua TEST_ITER22",
            "pickup_number": "24",
            "pickup_neighborhood": "Pituba",
            "pickup_complement": "",
            "location_reference": "TEST_ITER22",
            "pet_behavior_notes": "",
            "notes": f"TEST_ITER22_BLOCKED_SLOT_{uuid.uuid4().hex[:8]}",
        },
        timeout=30,
    )
    assert create_walk_resp.status_code == 400
    assert "Horário indisponível" in create_walk_resp.json().get("detail", "")
