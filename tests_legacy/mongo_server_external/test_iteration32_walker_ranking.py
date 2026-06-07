from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import bcrypt
import pytest
import requests
from dotenv import dotenv_values
from pymongo import MongoClient


# Module coverage: ranking inteligente de passeadores + elegibilidade + buffer mínimo + auth playbook crítico

CLIENT_CREDS = {"email": "cliente@petpasso.com", "password": "Cliente@123"}
ADMIN_EMAIL = "admin@petpasso.com"


def _mongo_db():
    env = dotenv_values("/app/backend/.env")
    mongo_url = str(env.get("MONGO_URL") or "").strip().strip('"')
    db_name = str(env.get("DB_NAME") or "").strip().strip('"')
    if not mongo_url or not db_name:
        pytest.skip("MONGO_URL/DB_NAME indisponíveis")
    client = MongoClient(mongo_url)
    return client, client[db_name]


def _login(base_url: str, email: str, password: str) -> requests.Session:
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


def _walk_doc(
    *,
    walker_user_id: str,
    walker_name: str,
    walk_date: str,
    walk_time: str,
    duration_minutes: int,
    status: str,
    rating: int | None = None,
    notes_prefix: str = "TEST_ITER32",
) -> dict[str, Any]:
    dt = datetime.strptime(f"{walk_date} {walk_time}", "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    now_iso = datetime.now(timezone.utc).isoformat()
    return {
        "id": f"{notes_prefix}_{uuid.uuid4().hex[:10]}",
        "pet_name": "TEST_ITER32_PET",
        "pet_ids": [],
        "shared_pet_names": [],
        "shared_client_names": [],
        "shared_owner_keys": [],
        "participant_user_ids": [],
        "client_user_id": None,
        "client_name": "TEST_ITER32_CLIENT",
        "walk_type": "Individual",
        "shared_context": None,
        "shared_approved": False,
        "shared_group": None,
        "walk_date": walk_date,
        "walk_time": walk_time,
        "duration_minutes": duration_minutes,
        "walker_id": f"partner-{walker_user_id}",
        "walker_user_id": walker_user_id,
        "walker_name": walker_name,
        "pickup_street": "Rua Teste",
        "pickup_number": "32",
        "pickup_neighborhood": "TEST_BAIRRO",
        "pickup_complement": "",
        "location_reference": "TEST_ITER32",
        "security_code": "1234",
        "did_pee": False,
        "did_poop": False,
        "rating": rating,
        "rating_comment": "",
        "summary_text": "",
        "pet_behavior_notes": "",
        "notes": f"{notes_prefix} auto",
        "motivoCancelamento": "",
        "tipoCancelamento": None,
        "penalidadePercentual": 0,
        "base_price": 45.0,
        "walker_payout": 33.75,
        "scheduled_start_at": dt.isoformat(),
        "walker_check_in_at": None,
        "client_confirmed_at": None,
        "tolerance_expires_at": None,
        "attendance_message": "",
        "charged_amount": 45.0,
        "walker_payout_amount": 33.75,
        "platform_retained_amount": 11.25,
        "client_refund_amount": 0.0,
        "decision_resolved_at": None,
        "decision_source": "",
        "walker_penalty_registered": False,
        "status": status,
        "photo_url": None,
        "walk_datetime_iso": dt.isoformat(),
        "created_at": now_iso,
        "updated_at": now_iso,
    }


@pytest.fixture(scope="module")
def ranking_env(base_url: str):
    test_date = "2026-04-20"  # segunda-feira
    client, db = _mongo_db()
    users = db.users
    walks = db.walks

    walker_specs: list[dict[str, Any]] = [
        {
            "slug": "premium",
            "quality_status": "ativo_premium",
            "region": "TEST_BAIRRO",
            "availability_days": ["seg"],
            "availability_start_time": "08:00",
            "availability_end_time": "12:00",
            "unavailable_until": None,
            "ratings": [5, 5, 5, 5, 5],
        },
        {
            "slug": "active",
            "quality_status": "ativo",
            "region": "TEST_BAIRRO",
            "availability_days": ["seg"],
            "availability_start_time": "08:00",
            "availability_end_time": "12:00",
            "unavailable_until": None,
            "ratings": [5, 5, 5],
        },
        {
            "slug": "missing_region",
            "quality_status": "ativo",
            "region": "",
            "availability_days": ["seg"],
            "availability_start_time": "08:00",
            "availability_end_time": "12:00",
            "unavailable_until": None,
            "ratings": [4, 4],
        },
        {
            "slug": "restricted",
            "quality_status": "restrito",
            "region": "TEST_BAIRRO",
            "availability_days": ["seg"],
            "availability_start_time": "08:00",
            "availability_end_time": "12:00",
            "unavailable_until": None,
            "ratings": [5],
        },
        {
            "slug": "suspended",
            "quality_status": "suspenso",
            "region": "TEST_BAIRRO",
            "availability_days": ["seg"],
            "availability_start_time": "08:00",
            "availability_end_time": "12:00",
            "unavailable_until": None,
            "ratings": [5],
        },
        {
            "slug": "no_compatible_schedule",
            "quality_status": "ativo",
            "region": "TEST_BAIRRO",
            "availability_days": ["ter"],
            "availability_start_time": "08:00",
            "availability_end_time": "12:00",
            "unavailable_until": None,
            "ratings": [5],
        },
        {
            "slug": "conflict_only",
            "quality_status": "ativo",
            "region": "TEST_BAIRRO",
            "availability_days": ["seg"],
            "availability_start_time": "09:00",
            "availability_end_time": "09:30",
            "unavailable_until": None,
            "ratings": [5],
        },
        {
            "slug": "marked_unavailable",
            "quality_status": "ativo",
            "region": "TEST_BAIRRO",
            "availability_days": ["seg"],
            "availability_start_time": "08:00",
            "availability_end_time": "12:00",
            "unavailable_until": (datetime.now(timezone.utc) + timedelta(days=2)).isoformat(),
            "ratings": [5, 4, 5],
        },
    ]

    created_user_ids: list[str] = []
    try:
        # cleanup pre-run
        users.delete_many({"email": {"$regex": r"^test_iter32_.*@petpasso\.com$"}})
        walks.delete_many({"notes": {"$regex": r"^TEST_ITER32"}})

        seeded: dict[str, dict[str, str]] = {}
        now_iso = datetime.now(timezone.utc).isoformat()
        for spec in walker_specs:
            walker_id = str(uuid.uuid4())
            created_user_ids.append(walker_id)
            email = f"test_iter32_{spec['slug']}@petpasso.com"
            full_name = f"TEST_ITER32 {spec['slug']}"
            users.insert_one(
                {
                    "id": walker_id,
                    "full_name": full_name,
                    "email": email,
                    "role": "passeador",
                    "isAdmin": False,
                    "isActive": True,
                    "password_hash": bcrypt.hashpw("TestIter32@123".encode("utf-8"), bcrypt.gensalt()).decode("utf-8"),
                    "region": spec["region"],
                    "quality_status": spec["quality_status"],
                    "quality_status_reason": "TEST_ITER32",
                    "availability_days": spec["availability_days"],
                    "availability_start_time": spec["availability_start_time"],
                    "availability_end_time": spec["availability_end_time"],
                    "availability_blocks": [],
                    "unavailable_until": spec["unavailable_until"],
                    "created_at": now_iso,
                    "updated_at": now_iso,
                }
            )
            seeded[spec["slug"]] = {"user_id": walker_id, "partner_id": f"partner-{walker_id}", "name": full_name}

            ratings = spec["ratings"]
            for idx, rating in enumerate(ratings):
                date_base = datetime.strptime(test_date, "%Y-%m-%d").replace(tzinfo=timezone.utc) - timedelta(days=idx + 1)
                walks.insert_one(
                    _walk_doc(
                        walker_user_id=walker_id,
                        walker_name=full_name,
                        walk_date=date_base.strftime("%Y-%m-%d"),
                        walk_time="08:00",
                        duration_minutes=30,
                        status="Finalizado",
                        rating=rating,
                    )
                )

        # hard conflict for conflict_only walker at requested slot 09:00
        conflict = seeded["conflict_only"]
        walks.insert_one(
            _walk_doc(
                walker_user_id=conflict["user_id"],
                walker_name=conflict["name"],
                walk_date=test_date,
                walk_time="09:00",
                duration_minutes=30,
                status="Agendado",
            )
        )

        client_session = _login(base_url, CLIENT_CREDS["email"], CLIENT_CREDS["password"])

        yield {
            "session": client_session,
            "seeded": seeded,
            "test_date": test_date,
        }

        client_session.close()
    finally:
        try:
            walks.delete_many({"notes": {"$regex": r"^TEST_ITER32"}})
            if created_user_ids:
                users.delete_many({"id": {"$in": created_user_ids}})
            users.delete_many({"email": {"$regex": r"^test_iter32_.*@petpasso\.com$"}})
        finally:
            client.close()


def test_auth_bcrypt_seed_hash_uses_2b_prefix():
    client, db = _mongo_db()
    try:
        admin = db.users.find_one({"email": ADMIN_EMAIL}, {"_id": 0, "password_hash": 1})
        assert admin is not None
        assert str(admin.get("password_hash") or "").startswith("$2b$")
    finally:
        client.close()


def test_auth_login_sets_http_only_cookies(base_url: str):
    response = requests.post(
        f"{base_url}/api/auth/login",
        json=CLIENT_CREDS,
        timeout=20,
    )
    assert response.status_code == 200
    set_cookie = (response.headers.get("set-cookie") or "").lower()
    assert "access_token=" in set_cookie
    assert "refresh_token=" in set_cookie
    assert "httponly" in set_cookie


def test_auth_lockout_after_five_failures(base_url: str):
    email = f"test_iter32_lock_{uuid.uuid4().hex[:8]}@example.com"
    session = requests.Session()
    try:
        for _ in range(5):
            fail = session.post(
                f"{base_url}/api/auth/login",
                json={"email": email, "password": "senha-invalida"},
                timeout=20,
            )
            assert fail.status_code == 401

        locked = session.post(
            f"{base_url}/api/auth/login",
            json={"email": email, "password": "senha-invalida"},
            timeout=20,
        )
        assert locked.status_code == 429
    finally:
        session.close()


def test_auth_cors_preflight_allows_explicit_origin_with_credentials(base_url: str):
    origin = base_url.rstrip("/")
    response = requests.options(
        f"{base_url}/api/auth/login",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
        timeout=20,
    )
    assert response.status_code in (200, 204)
    assert response.headers.get("access-control-allow-credentials") == "true"
    assert response.headers.get("access-control-allow-origin") == origin


def test_walker_eligibility_excludes_restricted_suspended_no_schedule_and_conflict(ranking_env: dict[str, Any], base_url: str):
    response = ranking_env["session"].get(
        f"{base_url}/api/walkers",
        params={
            "date": ranking_env["test_date"],
            "duration_minutes": 30,
            "preferred_time": "09:00",
            "neighborhood": "TEST_BAIRRO",
        },
        timeout=30,
    )
    assert response.status_code == 200, response.text
    walkers = response.json()

    names = {row.get("name") for row in walkers}
    assert "TEST_ITER32 restricted" not in names
    assert "TEST_ITER32 suspended" not in names
    assert "TEST_ITER32 no_compatible_schedule" not in names
    assert "TEST_ITER32 conflict_only" not in names


def test_unavailable_until_should_exclude_walker_from_client_list(ranking_env: dict[str, Any], base_url: str):
    response = ranking_env["session"].get(
        f"{base_url}/api/walkers",
        params={
            "date": ranking_env["test_date"],
            "duration_minutes": 30,
            "preferred_time": "09:00",
            "neighborhood": "TEST_BAIRRO",
        },
        timeout=30,
    )
    assert response.status_code == 200, response.text
    names = {row.get("name") for row in response.json()}
    assert "TEST_ITER32 marked_unavailable" not in names


def test_buffer_minimum_between_walks_is_15_minutes(ranking_env: dict[str, Any], base_url: str):
    conflict_walker = ranking_env["seeded"]["conflict_only"]
    response = ranking_env["session"].get(
        f"{base_url}/api/walkers/{conflict_walker['partner_id']}/availability-slots",
        params={
            "date": ranking_env["test_date"],
            "duration_minutes": 30,
        },
        timeout=30,
    )
    assert response.status_code == 200, response.text
    slots = response.json().get("available_slots") or []
    assert "08:15" in slots  # fronteira exata de 15 minutos permitida
    assert "08:30" not in slots  # cai dentro da janela de buffer
    assert "09:45" in slots  # primeiro horário válido após conflito + buffer


def test_ranking_order_prefers_status_then_time_then_region_for_test_walkers(ranking_env: dict[str, Any], base_url: str):
    response = ranking_env["session"].get(
        f"{base_url}/api/walkers",
        params={
            "date": ranking_env["test_date"],
            "duration_minutes": 30,
            "preferred_time": "09:00",
            "neighborhood": "TEST_BAIRRO",
        },
        timeout=30,
    )
    assert response.status_code == 200, response.text
    walkers = response.json()

    filtered = [row for row in walkers if str(row.get("name", "")).startswith("TEST_ITER32")]
    if len(filtered) < 3:
        pytest.skip("Poucos passeadores TEST_ITER32 visíveis para validar ordenação hierárquica")

    order_names = [row["name"] for row in filtered]
    assert order_names[0] == "TEST_ITER32 premium"
    assert order_names.index("TEST_ITER32 active") < order_names.index("TEST_ITER32 missing_region")


def test_top3_have_short_reason_and_after_top3_reason_is_empty(ranking_env: dict[str, Any], base_url: str):
    response = ranking_env["session"].get(
        f"{base_url}/api/walkers",
        params={
            "date": ranking_env["test_date"],
            "duration_minutes": 30,
            "preferred_time": "09:00",
            "neighborhood": "TEST_BAIRRO",
        },
        timeout=30,
    )
    assert response.status_code == 200, response.text
    walkers = response.json()
    if len(walkers) < 4:
        pytest.skip("Menos de 4 passeadores visíveis para validar regra top3")

    for row in walkers[:3]:
        reason = str(row.get("selection_reason") or "")
        assert reason
        assert len(reason) <= 40

    for row in walkers[3:]:
        assert str(row.get("selection_reason") or "") == ""


def test_client_fields_and_no_negative_status_exposure(ranking_env: dict[str, Any], base_url: str):
    response = ranking_env["session"].get(
        f"{base_url}/api/walkers",
        params={
            "date": ranking_env["test_date"],
            "duration_minutes": 30,
            "preferred_time": "09:00",
            "neighborhood": "TEST_BAIRRO",
        },
        timeout=30,
    )
    assert response.status_code == 200, response.text
    walkers = response.json()
    assert walkers

    for row in walkers:
        assert row.get("name")
        assert row.get("photo_url")
        assert "rating_avg" in row
        assert "rating_count" in row
        assert "region" in row
        assert "public_badge" in row
        assert str(row.get("quality_status") or "") not in {"restrito", "suspenso", "em_observacao"}
