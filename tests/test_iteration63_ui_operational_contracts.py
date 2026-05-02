from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests
from dotenv import dotenv_values
from pymongo import MongoClient


# Module coverage: walker kit endpoints, checklist payload/aliases, and schedule trust data contracts.

TEST_TAG = "TEST_ITER63"


def _mongo_db():
    backend_env = dotenv_values("/app/backend/.env")
    mongo_url = os.environ.get("MONGO_URL") or backend_env.get("MONGO_URL")
    db_name = os.environ.get("DB_NAME") or backend_env.get("DB_NAME")
    if not mongo_url or not db_name:
        pytest.skip("MONGO_URL/DB_NAME não configurados")
    client = MongoClient(str(mongo_url).strip().strip('"'))
    return client, client[str(db_name).strip().strip('"')]


def _login(base_url: str, email: str, password: str) -> requests.Session:
    session = requests.Session()
    response = session.post(
        f"{base_url}/api/auth/login",
        json={"email": email, "password": password},
        timeout=30,
    )
    if response.status_code != 200:
        session.close()
        pytest.skip(f"Login indisponível para {email}: {response.status_code}")
    body = response.json() if response.text else {}
    token = body.get("access_token")
    if not token:
        session.close()
        pytest.skip(f"Token ausente para {email}")
    session.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/json"})
    return session


def _checklist_payload() -> dict[str, bool]:
    return {
        "checklist_confirm_water": True,
        "checklist_confirm_bowl": True,
        "checklist_confirm_bags": True,
        "checklist_confirm_first_aid": True,
    }


def _create_seed_walk(db, walker_user_id: str, walker_name: str, walker_partner_id: str, client_user_id: str, client_name: str) -> str:
    walk_dt = datetime.now(timezone.utc) + timedelta(days=1)
    walk_id = f"{TEST_TAG}_WALK_{uuid.uuid4().hex[:10]}"
    row = {
        "id": walk_id,
        "pet_name": f"{TEST_TAG}_PET",
        "pet_ids": [],
        "shared_pet_names": [],
        "shared_client_names": [],
        "client_user_id": client_user_id,
        "client_name": client_name,
        "walk_type": "Individual",
        "shared_approved": False,
        "walk_date": walk_dt.strftime("%Y-%m-%d"),
        "walk_time": walk_dt.strftime("%H:%M"),
        "duration_minutes": 30,
        "walker_id": walker_partner_id,
        "walker_user_id": walker_user_id,
        "walker_name": walker_name,
        "pickup_street": "Rua TEST",
        "pickup_number": "123",
        "pickup_neighborhood": "Centro",
        "pickup_complement": "",
        "location_reference": TEST_TAG,
        "security_code": "1111",
        "did_pee": False,
        "did_poop": False,
        "rating_comment": "",
        "summary_text": "",
        "pet_behavior_notes": "",
        "notes": TEST_TAG,
        "motivoCancelamento": "",
        "tipoCancelamento": None,
        "penalidadePercentual": 0,
        "base_price": 39.9,
        "walker_payout": 29.9,
        "status": "Agendado",
        "walk_datetime_iso": walk_dt.isoformat(),
        "created_at": walk_dt.isoformat(),
        "updated_at": walk_dt.isoformat(),
    }
    db.walks.insert_one(row)
    return walk_id


@pytest.fixture(scope="module")
def scope(base_url: str):
    mongo_client, db = _mongo_db()
    walker = _login(base_url, "walker@petpasso.com", "Walker@123")
    client = _login(base_url, "cliente@petpasso.com", "Cliente@123")

    walker_me = walker.get(f"{base_url}/api/auth/me", timeout=30)
    client_me = client.get(f"{base_url}/api/auth/me", timeout=30)
    assert walker_me.status_code == 200, walker_me.text
    assert client_me.status_code == 200, client_me.text
    walker_user = walker_me.json()
    client_user = client_me.json()

    data = {
        "base_url": base_url,
        "db": db,
        "walker": walker,
        "client": client,
        "walker_user_id": str(walker_user.get("id") or ""),
        "walker_name": str(walker_user.get("full_name") or ""),
        "walker_partner_id": f"partner-{walker_user.get('id')}",
        "client_user_id": str(client_user.get("id") or ""),
        "client_name": str(client_user.get("full_name") or ""),
    }
    yield data

    db.walks.delete_many({"id": {"$regex": f"^{TEST_TAG}_"}})
    walker.close()
    client.close()
    mongo_client.close()


def test_walker_certified_kit_dashboard_contract(scope):
    walker = scope["walker"]
    base_url = scope["base_url"]

    kit = walker.get(f"{base_url}/api/walker/certified-kit", timeout=30)
    assert kit.status_code == 200, kit.text
    kit_data = kit.json()
    assert "kit_complete" in kit_data

    incentive = walker.get(f"{base_url}/api/walker/incentives/summary", timeout=30)
    assert incentive.status_code == 200, incentive.text
    incentive_data = incentive.json()
    assert "walker_level" in incentive_data
    assert "level_progress_percent" in incentive_data


def test_upload_and_remove_certified_kit_photo(scope):
    walker = scope["walker"]
    base_url = scope["base_url"]

    before = walker.get(f"{base_url}/api/walker/certified-kit", timeout=30)
    assert before.status_code == 200, before.text
    original_urls = list((before.json() or {}).get("kit_photo_urls") or [])

    upload = walker.post(
        f"{base_url}/api/walker/certified-kit/upload-photo",
        files={"file": ("iter63_kit.jpg", b"\xff\xd8\xff\xe0" + b"A" * 120, "image/jpeg")},
        timeout=30,
    )
    assert upload.status_code == 200, upload.text
    upload_data = upload.json()
    urls = upload_data.get("kit_photo_urls") or []
    assert len(urls) >= 1
    assert str(urls[-1]).startswith("/uploads/")

    remove_payload = {"kit_photo_urls": original_urls}
    remove = walker.patch(
        f"{base_url}/api/walker/certified-kit",
        json=remove_payload,
        timeout=30,
    )
    assert remove.status_code == 200, remove.text
    remove_data = remove.json()
    assert (remove_data.get("kit_photo_urls") or []) == original_urls


def test_checkin_and_start_accept_first_aid_flag(scope):
    db = scope["db"]
    base_url = scope["base_url"]
    walker = scope["walker"]
    client = scope["client"]

    walk_id = _create_seed_walk(
        db,
        walker_user_id=scope["walker_user_id"],
        walker_name=scope["walker_name"],
        walker_partner_id=scope["walker_partner_id"],
        client_user_id=scope["client_user_id"],
        client_name=scope["client_name"],
    )

    check_in = walker.post(
        f"{base_url}/api/walks/{walk_id}/check-in",
        json=_checklist_payload(),
        timeout=30,
    )
    assert check_in.status_code == 200, check_in.text

    validate = client.post(
        f"{base_url}/api/walks/{walk_id}/kit-checklist/check-in-validate",
        json=_checklist_payload(),
        timeout=30,
    )
    assert validate.status_code == 200, validate.text
    assert validate.json().get("kit_checklist_check_in_confirmed") is True

    start = walker.post(
        f"{base_url}/api/walks/{walk_id}/kit-checklist/start",
        json=_checklist_payload(),
        timeout=30,
    )
    assert start.status_code == 200, start.text
    assert start.json().get("kit_checklist_start_confirmed") is True


def test_walk_detail_has_checklist_alias_fields(scope):
    db = scope["db"]
    base_url = scope["base_url"]
    walker = scope["walker"]
    client = scope["client"]

    walk_id = _create_seed_walk(
        db,
        walker_user_id=scope["walker_user_id"],
        walker_name=scope["walker_name"],
        walker_partner_id=scope["walker_partner_id"],
        client_user_id=scope["client_user_id"],
        client_name=scope["client_name"],
    )

    assert walker.post(f"{base_url}/api/walks/{walk_id}/check-in", json=_checklist_payload(), timeout=30).status_code == 200
    assert client.post(
        f"{base_url}/api/walks/{walk_id}/kit-checklist/check-in-validate",
        json=_checklist_payload(),
        timeout=30,
    ).status_code == 200
    assert walker.post(f"{base_url}/api/walks/{walk_id}/kit-checklist/start", json=_checklist_payload(), timeout=30).status_code == 200

    detail = client.get(f"{base_url}/api/walks/{walk_id}", timeout=30)
    assert detail.status_code == 200, detail.text
    payload = detail.json()
    assert payload.get("kit_checklist_check_in_confirmed") is True
    assert payload.get("kit_checklist_start_confirmed") is True
    assert "checklist_validado_chegada" in payload
    assert "checklist_confirmado_inicio" in payload


def test_validate_arrival_endpoint_contract(scope):
    db = scope["db"]
    base_url = scope["base_url"]
    walker = scope["walker"]
    client = scope["client"]

    walk_id = _create_seed_walk(
        db,
        walker_user_id=scope["walker_user_id"],
        walker_name=scope["walker_name"],
        walker_partner_id=scope["walker_partner_id"],
        client_user_id=scope["client_user_id"],
        client_name=scope["client_name"],
    )
    assert walker.post(f"{base_url}/api/walks/{walk_id}/check-in", json=_checklist_payload(), timeout=30).status_code == 200

    validated = client.post(
        f"{base_url}/api/walks/{walk_id}/kit-checklist/check-in-validate",
        json=_checklist_payload(),
        timeout=30,
    )
    assert validated.status_code == 200, validated.text
    body = validated.json()
    assert body.get("kit_checklist_check_in_confirmed") is True


def test_schedule_trust_indicators_available_in_walkers_payload(scope):
    client = scope["client"]
    base_url = scope["base_url"]
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")

    walkers = client.get(
        f"{base_url}/api/walkers",
        params={"date": tomorrow, "duration_minutes": 30, "tipo_passeio": "padrao"},
        timeout=30,
    )
    assert walkers.status_code == 200, walkers.text
    rows = walkers.json() if isinstance(walkers.json(), list) else []
    assert len(rows) > 0
    first = rows[0]
    assert "walker_level" in first
    assert "kit_complete" in first
    assert "kit_photo_urls" in first


def test_auth_playbook_seed_password_hash_and_lockout(scope):
    base_url = scope["base_url"]
    backend_env = dotenv_values(Path("/app/backend/.env"))
    db = scope["db"]

    admin_row = db.users.find_one({"email": "admin@petpasso.com"}, {"_id": 0, "password_hash": 1})
    assert admin_row is not None
    assert str(admin_row.get("password_hash") or "").startswith("$2b$")

    test_ip = f"198.51.100.{int(uuid.uuid4().hex[:2], 16)}"
    identifier = f"{test_ip}:admin@petpasso.com"
    db.login_attempts.delete_many({"identifier": identifier})

    statuses: list[int] = []
    for _ in range(6):
        resp = requests.post(
            f"{base_url}/api/auth/login",
            json={"email": "admin@petpasso.com", "password": "wrong-pass"},
            headers={"x-forwarded-for": test_ip},
            timeout=30,
        )
        statuses.append(resp.status_code)
    assert statuses[:5] == [401, 401, 401, 401, 401]
    assert statuses[5] == 429
    db.login_attempts.delete_many({"identifier": identifier})

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


def test_seed_admin_password_works(scope):
    base_url = scope["base_url"]
    login = requests.post(
        f"{base_url}/api/auth/login",
        json={"email": "admin@petpasso.com", "password": "Admin@123"},
        timeout=30,
    )
    assert login.status_code == 200, login.text
