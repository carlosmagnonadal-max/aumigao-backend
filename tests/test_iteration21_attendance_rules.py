import os
import sys
import uuid
import asyncio
from datetime import date, datetime, timedelta, timezone

import bcrypt
import pytest
import requests
from dotenv import dotenv_values
from pymongo import MongoClient


# Module coverage: agenda buffer (25min), check-in/confirm actions, automatic attendance decisions, required walk fields

CLIENT_CREDS = {"email": "cliente@petpasso.com", "password": "Cliente@123"}
WALKER_CREDS = {"email": "walker@petpasso.com", "password": "Walker@123"}
ADMIN_CREDS = {"email": "admin@petpasso.com", "password": "Admin@123"}


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


def _create_test_pet(client_session: requests.Session, base_url: str, suffix: str) -> dict:
    response = client_session.post(
        f"{base_url}/api/pets",
        json={
            "pet_name": f"TEST_ITER21_PET_{suffix}",
            "behavioral_notes": "TEST comportamento",
            "photo_url": "",
            "owner_name": "TEST Owner",
            "gets_along_with_dogs": True,
            "accepts_shared_walk": True,
            "pet_size": "Médio",
            "energy_level": "Médio",
            "pulls_leash": False,
            "dog_behavior": "Neutro",
        },
        timeout=25,
    )
    assert response.status_code == 201, response.text
    return response.json()


def _weekday_index_from_key(day_key: str) -> int:
    mapping = {"seg": 0, "ter": 1, "qua": 2, "qui": 3, "sex": 4, "sab": 5, "dom": 6}
    return mapping[day_key]


def _get_walker_id_for_logged_walker(walker_session: requests.Session, base_url: str) -> str:
    me_response = walker_session.get(f"{base_url}/api/auth/me", timeout=25)
    assert me_response.status_code == 200, me_response.text
    walker_user = me_response.json()

    walkers_response = walker_session.get(f"{base_url}/api/walkers", timeout=25)
    assert walkers_response.status_code == 200, walkers_response.text
    walkers = walkers_response.json()

    by_id = f"partner-{walker_user['id']}"
    if any(w["id"] == by_id for w in walkers):
        return by_id

    same_name = [w for w in walkers if w.get("name") == walker_user.get("full_name")]
    if same_name:
        return same_name[0]["id"]

    pytest.skip("Passeador de teste não está disponível na lista de walkers")


def _pick_valid_date_and_slot(client_session: requests.Session, base_url: str, walker_id: str, duration: int = 30) -> tuple[str, str, list[str]]:
    walkers_response = client_session.get(f"{base_url}/api/walkers", timeout=25)
    assert walkers_response.status_code == 200
    walkers = walkers_response.json()
    walker = next((row for row in walkers if row.get("id") == walker_id), None)
    assert walker is not None

    days = walker.get("availability_days") or ["seg", "ter", "qua", "qui", "sex"]
    target_date = _next_date_for_weekday(_weekday_index_from_key(days[0]))

    slots_response = client_session.get(
        f"{base_url}/api/walkers/{walker_id}/availability-slots",
        params={"date": target_date, "duration_minutes": duration},
        timeout=25,
    )
    assert slots_response.status_code == 200, slots_response.text
    slots = slots_response.json().get("available_slots", [])
    assert len(slots) >= 1
    return target_date, slots[min(len(slots) // 2, len(slots) - 1)], slots


def _create_walk(
    client_session: requests.Session,
    base_url: str,
    pet: dict,
    walker_id: str,
    walk_date: str,
    walk_time: str,
    notes_suffix: str,
) -> dict:
    payload = {
        "pet_name": pet["pet_name"],
        "pet_id": pet["id"],
        "client_name": "TEST ITER21 Cliente",
        "walk_date": walk_date,
        "walk_time": walk_time,
        "duration_minutes": 30,
        "walk_type": "Individual",
        "walker_id": walker_id,
        "pickup_street": "Rua Teste",
        "pickup_number": "120",
        "pickup_neighborhood": "Pituba",
        "pickup_complement": "",
        "location_reference": "Próximo ao mercado",
        "pet_behavior_notes": "",
        "notes": f"TEST_ITER21_{notes_suffix}",
    }
    response = client_session.post(f"{base_url}/api/walks", json=payload, timeout=25)
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture(scope="session")
def mongo_db():
    backend_env = dotenv_values("/app/backend/.env")
    mongo_url = os.environ.get("MONGO_URL") or backend_env.get("MONGO_URL")
    db_name = os.environ.get("DB_NAME") or backend_env.get("DB_NAME")
    if not mongo_url or not db_name:
        pytest.skip("MONGO_URL/DB_NAME não configurados")

    client = MongoClient(str(mongo_url).strip().strip('"'))
    db = client[str(db_name).strip().strip('"')]
    yield db
    client.close()


def test_availability_slots_respect_25_min_buffer(base_url):
    client_session = _login(base_url, CLIENT_CREDS["email"], CLIENT_CREDS["password"])
    walker_session = _login(base_url, WALKER_CREDS["email"], WALKER_CREDS["password"])
    walker_id = _get_walker_id_for_logged_walker(walker_session, base_url)
    pet = _create_test_pet(client_session, base_url, "BUFFER")

    walk_date, target_slot, slots_before = _pick_valid_date_and_slot(client_session, base_url, walker_id, duration=30)
    created = _create_walk(client_session, base_url, pet, walker_id, walk_date, target_slot, "BUFFER")

    slots_after_response = client_session.get(
        f"{base_url}/api/walkers/{walker_id}/availability-slots",
        params={"date": walk_date, "duration_minutes": 30},
        timeout=25,
    )
    assert slots_after_response.status_code == 200
    slots_after = slots_after_response.json().get("available_slots", [])

    assert target_slot in slots_before
    assert target_slot not in slots_after

    start_dt = datetime.strptime(f"{walk_date} {target_slot}", "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    end_dt = start_dt + timedelta(minutes=30)
    for slot in slots_after:
        candidate_start = datetime.strptime(f"{walk_date} {slot}", "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        candidate_end = candidate_start + timedelta(minutes=30)
        has_conflict = candidate_start < (end_dt + timedelta(minutes=25)) and (candidate_end + timedelta(minutes=25)) > start_dt
        assert has_conflict is False

    assert created["status"] == "Agendado"
    client_session.close()
    walker_session.close()


def test_post_walks_blocks_conflicting_slot(base_url):
    client_session = _login(base_url, CLIENT_CREDS["email"], CLIENT_CREDS["password"])
    walker_session = _login(base_url, WALKER_CREDS["email"], WALKER_CREDS["password"])
    walker_id = _get_walker_id_for_logged_walker(walker_session, base_url)
    pet = _create_test_pet(client_session, base_url, "CONFLICT")

    walk_date, target_slot, _ = _pick_valid_date_and_slot(client_session, base_url, walker_id, duration=30)
    _create_walk(client_session, base_url, pet, walker_id, walk_date, target_slot, "CONFLICT_1")

    second_payload = {
        "pet_name": pet["pet_name"],
        "pet_id": pet["id"],
        "client_name": "TEST ITER21 Cliente",
        "walk_date": walk_date,
        "walk_time": target_slot,
        "duration_minutes": 30,
        "walk_type": "Individual",
        "walker_id": walker_id,
        "pickup_street": "Rua Teste",
        "pickup_number": "120",
        "pickup_neighborhood": "Pituba",
        "pickup_complement": "",
        "location_reference": "Próximo ao mercado",
        "pet_behavior_notes": "",
        "notes": "TEST_ITER21_CONFLICT_2",
    }
    second_response = client_session.post(f"{base_url}/api/walks", json=second_payload, timeout=25)
    assert second_response.status_code == 400
    assert "Horário indisponível" in second_response.json().get("detail", "")

    client_session.close()
    walker_session.close()


def test_walker_checkin_and_client_confirm_handover_flow(base_url):
    client_session = _login(base_url, CLIENT_CREDS["email"], CLIENT_CREDS["password"])
    walker_session = _login(base_url, WALKER_CREDS["email"], WALKER_CREDS["password"])
    walker_id = _get_walker_id_for_logged_walker(walker_session, base_url)
    pet = _create_test_pet(client_session, base_url, "CHECKIN")

    walk_date, target_slot, _ = _pick_valid_date_and_slot(client_session, base_url, walker_id, duration=30)
    created = _create_walk(client_session, base_url, pet, walker_id, walk_date, target_slot, "CHECKIN")

    check_in_response = walker_session.post(f"{base_url}/api/walks/{created['id']}/check-in", timeout=25)
    assert check_in_response.status_code == 200, check_in_response.text
    checked_in = check_in_response.json()
    assert checked_in["walker_check_in_at"] is not None
    assert checked_in["status"] == "Indo buscar o pet"

    confirm_response = client_session.post(f"{base_url}/api/walks/{created['id']}/confirm-handover", timeout=25)
    assert confirm_response.status_code == 200, confirm_response.text
    confirmed = confirm_response.json()
    assert confirmed["client_confirmed_at"] is not None
    assert confirmed["status"] == "Passeando agora"
    assert "Entrega confirmada" in confirmed.get("attendance_message", "")

    client_session.close()
    walker_session.close()


def test_create_walk_persists_required_temporal_and_financial_fields(base_url):
    client_session = _login(base_url, CLIENT_CREDS["email"], CLIENT_CREDS["password"])
    walker_session = _login(base_url, WALKER_CREDS["email"], WALKER_CREDS["password"])
    walker_id = _get_walker_id_for_logged_walker(walker_session, base_url)
    pet = _create_test_pet(client_session, base_url, "FIELDS")

    walk_date, target_slot, _ = _pick_valid_date_and_slot(client_session, base_url, walker_id, duration=30)
    created = _create_walk(client_session, base_url, pet, walker_id, walk_date, target_slot, "FIELDS")

    assert created["scheduled_start_at"] is not None
    assert created["tolerance_expires_at"] is not None
    assert created["status"] == "Agendado"
    assert isinstance(created.get("charged_amount"), (int, float))
    assert isinstance(created.get("walker_payout_amount"), (int, float))
    assert isinstance(created.get("platform_retained_amount"), (int, float))
    assert isinstance(created.get("client_refund_amount"), (int, float))

    persisted = client_session.get(f"{base_url}/api/walks/{created['id']}", timeout=25)
    assert persisted.status_code == 200
    walk = persisted.json()
    assert walk["scheduled_start_at"] is not None
    assert walk["status"] == "Agendado"

    client_session.close()
    walker_session.close()


def test_auto_case_client_no_show(base_url, mongo_db):
    client_session = _login(base_url, CLIENT_CREDS["email"], CLIENT_CREDS["password"])
    admin_session = _login(base_url, ADMIN_CREDS["email"], ADMIN_CREDS["password"])
    walker_session = _login(base_url, WALKER_CREDS["email"], WALKER_CREDS["password"])
    walker_id = _get_walker_id_for_logged_walker(walker_session, base_url)
    pet = _create_test_pet(client_session, base_url, "AUTO_CLIENT")
    walk_date, target_slot, _ = _pick_valid_date_and_slot(client_session, base_url, walker_id, duration=30)
    created = _create_walk(client_session, base_url, pet, walker_id, walk_date, target_slot, "AUTO_CLIENT")

    now = datetime.now(timezone.utc)
    past_start = now - timedelta(minutes=40)
    mongo_db.walks.update_one(
        {"id": created["id"]},
        {
            "$set": {
                "walk_date": past_start.strftime("%Y-%m-%d"),
                "walk_time": past_start.strftime("%H:%M"),
                "walk_datetime_iso": past_start.isoformat(),
                "scheduled_start_at": past_start.isoformat(),
                "walker_check_in_at": (past_start + timedelta(minutes=1)).isoformat(),
                "client_confirmed_at": None,
                "status": "Indo buscar o pet",
                "updated_at": now.isoformat(),
            }
        },
    )

    run_response = admin_session.post(f"{base_url}/api/automations/run", timeout=25)
    assert run_response.status_code == 200
    refreshed = client_session.get(f"{base_url}/api/walks/{created['id']}", timeout=25)
    assert refreshed.status_code == 200
    walk = refreshed.json()

    assert walk["status"] == "Não comparecimento do cliente"
    assert walk.get("decision_resolved_at") is not None
    assert walk.get("decision_source") in ("automation", "read")
    assert walk.get("charged_amount", 0) >= 0
    assert walk.get("walker_payout_amount", 0) >= 0

    client_session.close()
    admin_session.close()
    walker_session.close()


def test_auto_case_walker_no_show(base_url, mongo_db):
    client_session = _login(base_url, CLIENT_CREDS["email"], CLIENT_CREDS["password"])
    admin_session = _login(base_url, ADMIN_CREDS["email"], ADMIN_CREDS["password"])
    walker_session = _login(base_url, WALKER_CREDS["email"], WALKER_CREDS["password"])
    walker_id = _get_walker_id_for_logged_walker(walker_session, base_url)
    pet = _create_test_pet(client_session, base_url, "AUTO_WALKER")
    walk_date, target_slot, _ = _pick_valid_date_and_slot(client_session, base_url, walker_id, duration=30)
    created = _create_walk(client_session, base_url, pet, walker_id, walk_date, target_slot, "AUTO_WALKER")

    now = datetime.now(timezone.utc)
    past_start = now - timedelta(minutes=40)
    mongo_db.walks.update_one(
        {"id": created["id"]},
        {
            "$set": {
                "walk_date": past_start.strftime("%Y-%m-%d"),
                "walk_time": past_start.strftime("%H:%M"),
                "walk_datetime_iso": past_start.isoformat(),
                "scheduled_start_at": past_start.isoformat(),
                "walker_check_in_at": None,
                "client_confirmed_at": (past_start + timedelta(minutes=2)).isoformat(),
                "status": "Agendado",
                "updated_at": now.isoformat(),
            }
        },
    )

    run_response = admin_session.post(f"{base_url}/api/automations/run", timeout=25)
    assert run_response.status_code == 200
    refreshed = client_session.get(f"{base_url}/api/walks/{created['id']}", timeout=25)
    assert refreshed.status_code == 200
    walk = refreshed.json()

    assert walk["status"] == "Não comparecimento do passeador"
    assert walk.get("client_refund_amount", 0) >= 0
    assert walk.get("walker_payout_amount", 0) == 0

    client_session.close()
    admin_session.close()
    walker_session.close()


def test_auto_case_pending_review_without_records(base_url, mongo_db):
    client_session = _login(base_url, CLIENT_CREDS["email"], CLIENT_CREDS["password"])
    admin_session = _login(base_url, ADMIN_CREDS["email"], ADMIN_CREDS["password"])
    walker_session = _login(base_url, WALKER_CREDS["email"], WALKER_CREDS["password"])
    walker_id = _get_walker_id_for_logged_walker(walker_session, base_url)
    pet = _create_test_pet(client_session, base_url, "AUTO_PENDING")
    walk_date, target_slot, _ = _pick_valid_date_and_slot(client_session, base_url, walker_id, duration=30)
    created = _create_walk(client_session, base_url, pet, walker_id, walk_date, target_slot, "AUTO_PENDING")

    now = datetime.now(timezone.utc)
    past_start = now - timedelta(minutes=40)
    mongo_db.walks.update_one(
        {"id": created["id"]},
        {
            "$set": {
                "walk_date": past_start.strftime("%Y-%m-%d"),
                "walk_time": past_start.strftime("%H:%M"),
                "walk_datetime_iso": past_start.isoformat(),
                "scheduled_start_at": past_start.isoformat(),
                "walker_check_in_at": None,
                "client_confirmed_at": None,
                "status": "Agendado",
                "updated_at": now.isoformat(),
            }
        },
    )

    run_response = admin_session.post(f"{base_url}/api/automations/run", timeout=25)
    assert run_response.status_code == 200
    refreshed = client_session.get(f"{base_url}/api/walks/{created['id']}", timeout=25)
    assert refreshed.status_code == 200
    walk = refreshed.json()

    assert walk["status"] == "Pendente de análise"
    assert "análise" in (walk.get("attendance_message") or "").lower()

    client_session.close()
    admin_session.close()
    walker_session.close()


def test_operational_nomenclature_does_not_use_no_show(base_url):
    admin_session = _login(base_url, ADMIN_CREDS["email"], ADMIN_CREDS["password"])
    response = admin_session.get(f"{base_url}/api/admin/walks", timeout=25)
    assert response.status_code == 200
    statuses = {row.get("status", "") for row in response.json()}
    assert all("no-show" not in status.lower() for status in statuses)
    admin_session.close()


def test_auth_playbook_http_only_cookie_and_bcrypt(base_url, mongo_db):
    login_response = requests.post(
        f"{base_url}/api/auth/login",
        json={"email": ADMIN_CREDS["email"], "password": ADMIN_CREDS["password"]},
        timeout=25,
    )
    assert login_response.status_code == 200
    set_cookie = login_response.headers.get("set-cookie", "")
    assert "HttpOnly" in set_cookie
    assert "access_token" in set_cookie and "refresh_token" in set_cookie

    admin = mongo_db.users.find_one({"email": ADMIN_CREDS["email"]}, {"password_hash": 1})
    assert admin is not None
    assert str(admin.get("password_hash", "")).startswith("$2b$")


def test_auth_playbook_bruteforce_lockout_after_5_failures(base_url):
    lock_email = f"test_iter21_lock_{uuid.uuid4().hex[:8]}@petpasso.com"
    for _ in range(5):
        response = requests.post(
            f"{base_url}/api/auth/login",
            json={"email": lock_email, "password": "wrong-pass"},
            timeout=25,
        )
        assert response.status_code in (401, 429)

    blocked = requests.post(
        f"{base_url}/api/auth/login",
        json={"email": lock_email, "password": "wrong-pass"},
        timeout=25,
    )
    assert blocked.status_code == 429


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


def test_auth_playbook_seed_admin_updates_existing_password_hash(base_url, mongo_db):
    admin_email = ADMIN_CREDS["email"]
    admin_password = ADMIN_CREDS["password"]
    mutated_hash = bcrypt.hashpw("Changed@999".encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

    mongo_db.login_attempts.delete_many({"identifier": {"$regex": f":{admin_email}$"}})
    mongo_db.users.update_one({"email": admin_email}, {"$set": {"password_hash": mutated_hash}})

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
    mongo_db.login_attempts.delete_many({"identifier": {"$regex": f":{admin_email}$"}})

    post_seed_login = requests.post(
        f"{base_url}/api/auth/login",
        json={"email": admin_email, "password": admin_password},
        timeout=25,
    )
    assert post_seed_login.status_code == 200
