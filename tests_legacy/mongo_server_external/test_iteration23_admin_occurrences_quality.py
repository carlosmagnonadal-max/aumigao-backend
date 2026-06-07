from __future__ import annotations

import uuid
from datetime import date, timedelta
from typing import Any

import pytest
import requests
from dotenv import dotenv_values
from pymongo import MongoClient


# Module coverage: auth playbook checks (cookies/cors/lockout/hash) + admin occurrences/worker quality + walk rating rules


ADMIN_CREDS = {"email": "admin@petpasso.com", "password": "Admin@123"}
CLIENT_CREDS = {"email": "cliente@petpasso.com", "password": "Cliente@123"}
WALKER_CREDS = {"email": "walker@petpasso.com", "password": "Walker@123"}


def _login_session(base_url: str, *, email: str, password: str) -> requests.Session:
    session = requests.Session()
    session.headers.update({"Accept": "application/json", "Content-Type": "application/json"})
    response = session.post(f"{base_url}/api/auth/login", json={"email": email, "password": password}, timeout=30)
    assert response.status_code == 200, response.text
    token = response.json().get("access_token")
    assert token
    session.headers.update({"Authorization": f"Bearer {token}"})
    return session


def _iso_date(delta_days: int) -> str:
    return (date.today() + timedelta(days=delta_days)).strftime("%Y-%m-%d")


def _resolve_walker_id(client_session: requests.Session, base_url: str) -> str:
    walkers_resp = client_session.get(f"{base_url}/api/walkers", timeout=30)
    assert walkers_resp.status_code == 200, walkers_resp.text
    walkers = walkers_resp.json()
    assert isinstance(walkers, list) and walkers

    # prefer seeded walker@petpasso.com identity
    preferred = [row for row in walkers if row.get("id") in {"walker-1", "partner-walker"}]
    if preferred:
        return preferred[0]["id"]
    return walkers[0]["id"]


def _find_slot(client_session: requests.Session, base_url: str, walker_id: str, duration_minutes: int = 30) -> tuple[str, str]:
    for delta in range(2, 28):
        target = _iso_date(delta)
        slots_resp = client_session.get(
            f"{base_url}/api/walkers/{walker_id}/availability-slots",
            params={"date": target, "duration_minutes": duration_minutes},
            timeout=30,
        )
        if slots_resp.status_code != 200:
            continue
        slots = slots_resp.json().get("available_slots", [])
        if slots:
            return target, slots[0]
    pytest.skip("Sem slots disponíveis para criar passeio de teste")


def _ensure_pet(client_session: requests.Session, base_url: str) -> tuple[dict[str, Any], str | None]:
    pets_resp = client_session.get(f"{base_url}/api/pets", timeout=30)
    assert pets_resp.status_code == 200, pets_resp.text
    pets = pets_resp.json()
    if pets:
        return pets[0], None

    suffix = uuid.uuid4().hex[:8]
    payload = {
        "pet_name": f"TEST_ITER23_PET_{suffix}",
        "behavioral_notes": "TEST_ITER23 notes",
        "photo_url": "",
        "owner_name": "TEST_ITER23 Owner",
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


def _create_walk_for_tests(client_session: requests.Session, base_url: str, pet: dict[str, Any], walker_id: str) -> dict[str, Any]:
    target_date, slot = _find_slot(client_session, base_url, walker_id, duration_minutes=30)
    payload = {
        "pet_name": pet["pet_name"],
        "pet_id": pet["id"],
        "client_name": "TEST_ITER23 Cliente",
        "walk_date": target_date,
        "walk_time": slot,
        "duration_minutes": 30,
        "walk_type": "Individual",
        "walker_id": walker_id,
        "pickup_street": "Rua TEST_ITER23",
        "pickup_number": "23",
        "pickup_neighborhood": "Pituba",
        "pickup_complement": "",
        "location_reference": "TEST_ITER23",
        "pet_behavior_notes": "TEST_ITER23 behavior",
        "notes": f"TEST_ITER23_{uuid.uuid4().hex[:8]}",
    }
    response = client_session.post(f"{base_url}/api/walks", json=payload, timeout=30)
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture
def sessions(base_url: str):
    admin = _login_session(base_url, email=ADMIN_CREDS["email"], password=ADMIN_CREDS["password"])
    client = _login_session(base_url, email=CLIENT_CREDS["email"], password=CLIENT_CREDS["password"])
    walker = _login_session(base_url, email=WALKER_CREDS["email"], password=WALKER_CREDS["password"])

    walker_id = _resolve_walker_id(client, base_url)
    pet, created_pet_id = _ensure_pet(client, base_url)

    yield {
        "admin": admin,
        "client": client,
        "walker": walker,
        "walker_id": walker_id,
        "pet": pet,
        "created_pet_id": created_pet_id,
    }

    if created_pet_id:
        client.delete(f"{base_url}/api/pets/{created_pet_id}", timeout=20)
    admin.close()
    client.close()
    walker.close()


def test_auth_cookies_http_only_and_cors_preflight(base_url: str):
    login_resp = requests.post(
        f"{base_url}/api/auth/login",
        json=ADMIN_CREDS,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        timeout=30,
    )
    assert login_resp.status_code == 200, login_resp.text
    set_cookie = login_resp.headers.get("set-cookie", "")
    assert "access_token=" in set_cookie
    assert "refresh_token=" in set_cookie
    assert "HttpOnly" in set_cookie

    preflight = requests.options(
        f"{base_url}/api/auth/login",
        headers={
            "Origin": base_url,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
        timeout=30,
    )
    assert preflight.status_code in (200, 204)
    assert preflight.headers.get("Access-Control-Allow-Credentials", "").lower() == "true"
    assert preflight.headers.get("Access-Control-Allow-Origin") not in {"", "*"}


def test_auth_lockout_after_five_failed_attempts(base_url: str):
    email = f"lockout_iter23_{uuid.uuid4().hex[:6]}@petpasso.com"
    payload = {"email": email, "password": "wrong-password"}

    statuses = []
    for _ in range(6):
        response = requests.post(f"{base_url}/api/auth/login", json=payload, timeout=30)
        statuses.append(response.status_code)

    assert statuses[:5] == [401, 401, 401, 401, 401]
    assert statuses[5] == 429


def test_auth_seeded_admin_hash_uses_bcrypt_2b_prefix():
    values = dotenv_values("/app/backend/.env")
    mongo_url = values.get("MONGO_URL")
    db_name = values.get("DB_NAME")
    if not mongo_url or not db_name:
        pytest.skip("MONGO_URL/DB_NAME indisponíveis para validação de hash")

    client = MongoClient(str(mongo_url).strip().strip('"'))
    try:
        row = client[str(db_name).strip().strip('"')].users.find_one({"email": ADMIN_CREDS["email"]}, {"_id": 0, "password_hash": 1})
        assert row and isinstance(row.get("password_hash"), str)
        assert row["password_hash"].startswith("$2b$")
    finally:
        client.close()


def test_admin_occurrence_actions_reverse_rule_and_audit_log(base_url: str, sessions: dict[str, Any]):
    admin = sessions["admin"]
    client = sessions["client"]
    pet = sessions["pet"]
    walker_id = sessions["walker_id"]

    walk = _create_walk_for_tests(client, base_url, pet, walker_id)
    walk_id = walk["id"]

    # Force a no-show status first to get financial fields initialized on occurrence timeline
    status_resp = admin.patch(
        f"{base_url}/api/admin/walks/{walk_id}/status",
        json={"status": "Não comparecimento do passeador"},
        timeout=30,
    )
    assert status_resp.status_code == 200, status_resp.text

    initial = admin.get(f"{base_url}/api/admin/occurrences", timeout=30)
    assert initial.status_code == 200, initial.text
    before = next(item for item in initial.json() if item["id"] == walk_id)

    reverse_resp = admin.post(
        f"{base_url}/api/admin/occurrences/{walk_id}/action",
        json={"action": "reverse_decision", "note": "TEST_ITER23 reverse"},
        timeout=30,
    )
    assert reverse_resp.status_code == 200, reverse_resp.text
    reversed_row = reverse_resp.json()

    assert reversed_row["occurrence_status"] == "pendente_analise_reaberta"
    assert reversed_row["walk_status"] == "Pendente de análise"
    assert reversed_row["charged_amount"] == pytest.approx(before["charged_amount"], abs=0.01)
    assert reversed_row["walker_payout_amount"] == pytest.approx(before["walker_payout_amount"], abs=0.01)
    assert reversed_row["platform_retained_amount"] == pytest.approx(before["platform_retained_amount"], abs=0.01)
    assert reversed_row["client_refund_amount"] == pytest.approx(before["client_refund_amount"], abs=0.01)

    last_log = reversed_row["logs"][-1]
    assert last_log["action"] == "reverse_decision"
    assert last_log["performed_by_id"]
    assert last_log["performed_by_name"]
    assert last_log["timestamp"]
    assert isinstance(last_log["before_values"], dict)
    assert isinstance(last_log["after_values"], dict)

    filtered = admin.get(
        f"{base_url}/api/admin/occurrences",
        params={"status": "pendente_analise_reaberta"},
        timeout=30,
    )
    assert filtered.status_code == 200, filtered.text
    assert any(item["id"] == walk_id for item in filtered.json())


def test_admin_occurrence_actions_set_resolved_note_and_dispute_states(base_url: str, sessions: dict[str, Any]):
    admin = sessions["admin"]
    client = sessions["client"]
    pet = sessions["pet"]
    walker_id = sessions["walker_id"]

    walk = _create_walk_for_tests(client, base_url, pet, walker_id)
    walk_id = walk["id"]

    steps = [
        ({"action": "open_financial_dispute", "note": "TEST_ITER23 open"}, "disputa_financeira_aberta"),
        (
            {
                "action": "resolve_financial_dispute",
                "note": "TEST_ITER23 resolve",
                "refund_amount": 10.0,
                "payout_amount": 15.0,
                "retained_amount": 4.9,
            },
            "disputa_financeira_resolvida",
        ),
        ({"action": "add_internal_note", "note": "TEST_ITER23 note"}, None),
        ({"action": "mark_resolved", "note": "TEST_ITER23 resolved"}, "resolvido"),
        ({"action": "mark_unresolved", "note": "TEST_ITER23 unresolved"}, "nao_resolvido"),
    ]

    latest = None
    for payload, expected_status in steps:
        resp = admin.post(f"{base_url}/api/admin/occurrences/{walk_id}/action", json=payload, timeout=30)
        assert resp.status_code == 200, resp.text
        latest = resp.json()
        if expected_status:
            assert latest["occurrence_status"] == expected_status

    assert latest is not None
    assert "TEST_ITER23 note" in latest["internal_note"]


def test_admin_walker_performance_metrics_and_actions(base_url: str, sessions: dict[str, Any]):
    admin = sessions["admin"]

    perf_resp = admin.get(f"{base_url}/api/admin/walkers/performance", timeout=30)
    assert perf_resp.status_code == 200, perf_resp.text
    rows = perf_resp.json()
    assert isinstance(rows, list) and rows

    sample = rows[0]
    for field in [
        "rating_avg",
        "rating_count",
        "completed_walks",
        "severe_delay_rate",
        "no_show_rate",
        "cancel_rate",
        "can_be_featured_by_rule",
    ]:
        assert field in sample

    walker_id = sample["user_id"]

    warn_resp = admin.post(
        f"{base_url}/api/admin/walkers/{walker_id}/action",
        json={"action": "warn", "note": "TEST_ITER23 warn"},
        timeout=30,
    )
    assert warn_resp.status_code == 200, warn_resp.text
    assert warn_resp.json()["operational_status"] == "observacao"

    suspend_resp = admin.post(
        f"{base_url}/api/admin/walkers/{walker_id}/action",
        json={"action": "suspend", "note": "TEST_ITER23 suspend"},
        timeout=30,
    )
    assert suspend_resp.status_code == 200, suspend_resp.text
    assert suspend_resp.json()["operational_status"] == "suspenso"

    reactivate_resp = admin.post(
        f"{base_url}/api/admin/walkers/{walker_id}/action",
        json={"action": "reactivate", "note": "TEST_ITER23 reactivate"},
        timeout=30,
    )
    assert reactivate_resp.status_code == 200, reactivate_resp.text
    assert reactivate_resp.json()["operational_status"] == "ativo"

    ineligible = next((row for row in rows if not row.get("can_be_featured_by_rule")), None)
    if ineligible:
        feature_fail = admin.post(
            f"{base_url}/api/admin/walkers/{ineligible['user_id']}/action",
            json={"action": "feature_premium", "note": "TEST_ITER23 feature check"},
            timeout=30,
        )
        assert feature_fail.status_code == 400


def test_walk_rating_only_after_finished_and_only_once_for_client(base_url: str, sessions: dict[str, Any]):
    admin = sessions["admin"]
    client = sessions["client"]
    pet = sessions["pet"]
    walker_id = sessions["walker_id"]

    walk = _create_walk_for_tests(client, base_url, pet, walker_id)
    walk_id = walk["id"]

    before_finish = client.patch(
        f"{base_url}/api/walks/{walk_id}/rating",
        json={"rating": 5, "comment": "TEST_ITER23 before finish"},
        timeout=30,
    )
    assert before_finish.status_code == 400

    finish_resp = admin.patch(
        f"{base_url}/api/admin/walks/{walk_id}/status",
        json={"status": "Finalizado"},
        timeout=30,
    )
    assert finish_resp.status_code == 200, finish_resp.text

    first_rating = client.patch(
        f"{base_url}/api/walks/{walk_id}/rating",
        json={"rating": 4, "comment": "TEST_ITER23 first rating"},
        timeout=30,
    )
    assert first_rating.status_code == 200, first_rating.text
    assert first_rating.json()["rating"] == 4

    second_rating = client.patch(
        f"{base_url}/api/walks/{walk_id}/rating",
        json={"rating": 5, "comment": "TEST_ITER23 second rating"},
        timeout=30,
    )
    assert second_rating.status_code == 400
    assert "já foi avaliado" in second_rating.text
