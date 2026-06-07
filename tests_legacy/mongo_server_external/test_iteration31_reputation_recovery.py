from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
import pytest
import requests
from dotenv import dotenv_values
from pymongo import MongoClient


# Module coverage: reputação Uber-style + monitoramento/recuperação + ordenação pública

ADMIN_CREDS = {"email": "admin@petpasso.com", "password": "Admin@123"}
CLIENT_CREDS = {"email": "cliente@petpasso.com", "password": "Cliente@123"}
TEST_WALKER_EMAIL = "test_iter31_walker@petpasso.com"
TEST_WALKER_PASSWORD = "TestIter31@123"
TEST_WALKER_NAME = "TEST ITER31 Walker"


def _mongo_collection(name: str):
    env = dotenv_values("/app/backend/.env")
    mongo_url = str(env.get("MONGO_URL") or "").strip().strip('"')
    db_name = str(env.get("DB_NAME") or "").strip().strip('"')
    if not mongo_url or not db_name:
        pytest.skip("MONGO_URL/DB_NAME indisponíveis")
    client = MongoClient(mongo_url)
    return client, client[db_name][name]


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


def _ensure_test_walker() -> dict[str, str]:
    mongo_client, users = _mongo_collection("users")
    try:
        existing = users.find_one({"email": TEST_WALKER_EMAIL}, {"_id": 0})
        now_iso = datetime.now(timezone.utc).isoformat()
        password_hash = bcrypt.hashpw(TEST_WALKER_PASSWORD.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
        payload = {
            "full_name": TEST_WALKER_NAME,
            "role": "passeador",
            "isAdmin": False,
            "isActive": True,
            "password_hash": password_hash,
            "region": "Salvador/BA",
            "availability_days": ["seg", "ter", "qua", "qui", "sex", "sab"],
            "availability_start_time": "07:00",
            "availability_end_time": "20:00",
            "quality_status": "ativo",
            "quality_status_reason": "Sem observações",
            "updated_at": now_iso,
        }
        if existing:
            users.update_one({"id": existing["id"]}, {"$set": payload})
            return {"id": existing["id"], "full_name": TEST_WALKER_NAME}

        walker_id = str(uuid.uuid4())
        users.insert_one(
            {
                "id": walker_id,
                "email": TEST_WALKER_EMAIL,
                "permissions": {},
                "created_by": "test_iteration31",
                "created_at": now_iso,
                "last_active_at": now_iso,
                "quality_monitoring": {
                    "active": False,
                    "severity": "padrao",
                    "target_walks": 7,
                    "completed_walks": 0,
                    "reset_count": 0,
                    "severe_delay_incidents": 0,
                    "course_completed": False,
                    "quiz_passed": False,
                    "quiz_attempts": 0,
                    "consecutive_quiz_failures": 0,
                    "review_recommended": False,
                },
                "quality_history": [],
                **payload,
            }
        )
        return {"id": walker_id, "full_name": TEST_WALKER_NAME}
    finally:
        mongo_client.close()


def _cleanup_test_walker_data(walker_user_id: str):
    mongo_client, users = _mongo_collection("users")
    walks = mongo_client[users.database.name]["walks"]
    try:
        walks.delete_many({"notes": {"$regex": r"^TEST_ITER31"}})
        users.delete_one({"id": walker_user_id})
    finally:
        mongo_client.close()


def _walk_document(
    *,
    walker_user_id: str,
    walker_name: str,
    status: str,
    dt: datetime,
    rating: int | None = None,
    severe_delay: bool = False,
    walk_suffix: str = "",
) -> dict[str, Any]:
    now_iso = datetime.now(timezone.utc).isoformat()
    return {
        "id": f"TEST_ITER31_{walk_suffix or uuid.uuid4().hex[:10]}",
        "pet_name": "TEST_ITER31_PET",
        "pet_ids": [],
        "shared_pet_names": [],
        "shared_client_names": [],
        "shared_owner_keys": [],
        "participant_user_ids": [],
        "client_user_id": None,
        "client_name": "TEST_ITER31_CLIENT",
        "walk_type": "Individual",
        "shared_context": None,
        "shared_approved": False,
        "shared_group": None,
        "walk_date": dt.strftime("%Y-%m-%d"),
        "walk_time": dt.strftime("%H:%M"),
        "duration_minutes": 30,
        "walker_id": f"partner-{walker_user_id}",
        "walker_user_id": walker_user_id,
        "walker_name": walker_name,
        "pickup_street": "Rua Teste",
        "pickup_number": "31",
        "pickup_neighborhood": "Pituba",
        "pickup_complement": "",
        "location_reference": "TEST_ITER31",
        "security_code": "1234",
        "did_pee": False,
        "did_poop": False,
        "rating": rating,
        "rating_comment": "",
        "summary_text": "",
        "pet_behavior_notes": "",
        "notes": "TEST_ITER31 auto",
        "motivoCancelamento": "",
        "tipoCancelamento": None,
        "penalidadePercentual": 0,
        "base_price": 45.0,
        "walker_payout": 33.75,
        "scheduled_start_at": dt.isoformat(),
        "walker_check_in_at": (dt + timedelta(minutes=20)).isoformat() if severe_delay else None,
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


def _replace_walker_walks(walker_user_id: str, walks: list[dict[str, Any]]):
    mongo_client, db_walks = _mongo_collection("walks")
    try:
        db_walks.delete_many({"walker_user_id": walker_user_id, "notes": {"$regex": r"^TEST_ITER31"}})
        if walks:
            db_walks.insert_many(walks)
    finally:
        mongo_client.close()


def _trigger_recalculation(admin_session: requests.Session, base_url: str):
    response = admin_session.get(f"{base_url}/api/admin/walkers/performance", timeout=30)
    assert response.status_code == 200, response.text


def _get_walker_user(walker_user_id: str) -> dict[str, Any]:
    mongo_client, users = _mongo_collection("users")
    try:
        row = users.find_one({"id": walker_user_id}, {"_id": 0})
        assert row is not None
        return row
    finally:
        mongo_client.close()


@pytest.fixture(scope="module")
def quality_env(base_url: str):
    walker = _ensure_test_walker()
    admin_session = _login(base_url, ADMIN_CREDS["email"], ADMIN_CREDS["password"])
    client_session = _login(base_url, CLIENT_CREDS["email"], CLIENT_CREDS["password"])
    walker_session = _login(base_url, TEST_WALKER_EMAIL, TEST_WALKER_PASSWORD)

    yield {
        "admin": admin_session,
        "client": client_session,
        "walker": walker_session,
        "walker_user_id": walker["id"],
        "walker_name": walker["full_name"],
        "walker_partner_id": f"partner-{walker['id']}",
    }

    admin_session.close()
    client_session.close()
    walker_session.close()
    _cleanup_test_walker_data(walker["id"])


def test_bcrypt_hash_prefix_for_seed_admin_is_2b():
    mongo_client, users = _mongo_collection("users")
    try:
        admin = users.find_one({"email": "admin@petpasso.com"}, {"_id": 0, "password_hash": 1})
        assert admin is not None
        assert str(admin.get("password_hash") or "").startswith("$2b$")
    finally:
        mongo_client.close()


def test_auth_login_sets_http_only_cookies(base_url: str):
    response = requests.post(
        f"{base_url}/api/auth/login",
        json={"email": CLIENT_CREDS["email"], "password": CLIENT_CREDS["password"]},
        timeout=20,
    )
    assert response.status_code == 200
    lowered = (response.headers.get("set-cookie") or "").lower()
    assert "access_token=" in lowered
    assert "refresh_token=" in lowered
    assert "httponly" in lowered


def test_auth_lockout_after_five_invalid_attempts(base_url: str):
    session = requests.Session()
    email = f"test_iter31_lockout_{uuid.uuid4().hex[:8]}@example.com"
    for _ in range(5):
        response = session.post(
            f"{base_url}/api/auth/login",
            json={"email": email, "password": "senha-invalida"},
            timeout=20,
        )
        assert response.status_code == 401
    sixth = session.post(
        f"{base_url}/api/auth/login",
        json={"email": email, "password": "senha-invalida"},
        timeout=20,
    )
    assert sixth.status_code == 429
    session.close()


def test_cors_preflight_auth_login_allows_credentials_with_explicit_origin(base_url: str):
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


def test_premium_requires_zero_recent_no_show_and_zero_recent_severe_delay(base_url: str, quality_env: dict[str, Any]):
    walker_user_id = quality_env["walker_user_id"]
    walker_name = quality_env["walker_name"]
    now = datetime.now(timezone.utc)

    premium_walks = [
        _walk_document(
            walker_user_id=walker_user_id,
            walker_name=walker_name,
            status="Finalizado",
            dt=now - timedelta(days=index + 1),
            rating=5,
            walk_suffix=f"PREMIUM_OK_{index}",
        )
        for index in range(10)
    ]
    _replace_walker_walks(walker_user_id, premium_walks)
    _trigger_recalculation(quality_env["admin"], base_url)
    row = _get_walker_user(walker_user_id)
    assert row.get("quality_status") == "ativo_premium"

    with_severe = premium_walks + [
        _walk_document(
            walker_user_id=walker_user_id,
            walker_name=walker_name,
            status="Finalizado",
            dt=now + timedelta(minutes=1),
            rating=5,
            severe_delay=True,
            walk_suffix="PREMIUM_BLOCK_SEVERE",
        )
    ]
    _replace_walker_walks(walker_user_id, with_severe)
    _trigger_recalculation(quality_env["admin"], base_url)
    row_after = _get_walker_user(walker_user_id)
    assert row_after.get("quality_status") != "ativo_premium"


def test_restricted_when_recent_rating_below_3_8_with_five_or_more_reviews(base_url: str, quality_env: dict[str, Any]):
    walker_user_id = quality_env["walker_user_id"]
    walker_name = quality_env["walker_name"]
    now = datetime.now(timezone.utc)

    ratings = [4, 4, 4, 3, 3]
    walks = [
        _walk_document(
            walker_user_id=walker_user_id,
            walker_name=walker_name,
            status="Finalizado",
            dt=now - timedelta(days=index + 1),
            rating=rating,
            walk_suffix=f"RECENT_LOW_{index}",
        )
        for index, rating in enumerate(ratings)
    ]
    _replace_walker_walks(walker_user_id, walks)
    _trigger_recalculation(quality_env["admin"], base_url)
    row = _get_walker_user(walker_user_id)
    assert row.get("quality_status") == "restrito"


def test_restricted_when_two_recent_complaint_ratings(base_url: str, quality_env: dict[str, Any]):
    walker_user_id = quality_env["walker_user_id"]
    walker_name = quality_env["walker_name"]
    now = datetime.now(timezone.utc)

    ratings = [5, 5, 5, 5, 2, 1]
    walks = [
        _walk_document(
            walker_user_id=walker_user_id,
            walker_name=walker_name,
            status="Finalizado",
            dt=now - timedelta(days=index + 1),
            rating=rating,
            walk_suffix=f"COMPLAINT_{index}",
        )
        for index, rating in enumerate(ratings)
    ]
    _replace_walker_walks(walker_user_id, walks)
    _trigger_recalculation(quality_env["admin"], base_url)
    row = _get_walker_user(walker_user_id)
    assert row.get("quality_status") == "restrito"


def test_rating_based_negative_rules_only_apply_with_five_or_more_reviews(base_url: str, quality_env: dict[str, Any]):
    walker_user_id = quality_env["walker_user_id"]
    walker_name = quality_env["walker_name"]
    now = datetime.now(timezone.utc)

    walks = [
        _walk_document(
            walker_user_id=walker_user_id,
            walker_name=walker_name,
            status="Finalizado",
            dt=now - timedelta(days=index + 1),
            rating=1,
            walk_suffix=f"UNDER_FIVE_{index}",
        )
        for index in range(4)
    ]
    _replace_walker_walks(walker_user_id, walks)
    _trigger_recalculation(quality_env["admin"], base_url)
    row = _get_walker_user(walker_user_id)
    assert int((row.get("quality_metrics") or {}).get("rating_count", 0)) == 4
    assert row.get("quality_status") == "ativo"


def test_monitoring_resets_when_recent_rating_drops_below_4_after_attendance_event(base_url: str, quality_env: dict[str, Any]):
    walker_user_id = quality_env["walker_user_id"]
    walker_name = quality_env["walker_name"]
    walker_session = quality_env["walker"]
    now = datetime.now(timezone.utc)

    low_rating_history = [
        _walk_document(
            walker_user_id=walker_user_id,
            walker_name=walker_name,
            status="Finalizado",
            dt=now - timedelta(days=index + 2),
            rating=3,
            walk_suffix=f"RESET_RATING_{index}",
        )
        for index in range(5)
    ]
    trigger_walk = _walk_document(
        walker_user_id=walker_user_id,
        walker_name=walker_name,
        status="Agendado",
        dt=now + timedelta(hours=2),
        rating=None,
        walk_suffix="RESET_TRIGGER_CHECKIN",
    )
    _replace_walker_walks(walker_user_id, low_rating_history + [trigger_walk])

    mongo_client, users = _mongo_collection("users")
    try:
        users.update_one(
            {"id": walker_user_id},
            {
                "$set": {
                    "isActive": True,
                    "quality_status": "restrito",
                    "quality_monitoring": {
                        "active": True,
                        "severity": "padrao",
                        "target_walks": 7,
                        "completed_walks": 3,
                        "reset_count": 0,
                        "severe_delay_incidents": 0,
                        "course_completed": True,
                        "quiz_passed": True,
                        "quiz_attempts": 1,
                        "consecutive_quiz_failures": 0,
                        "review_recommended": False,
                    },
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            },
        )
    finally:
        mongo_client.close()

    checkin = walker_session.post(f"{base_url}/api/walks/{trigger_walk['id']}/check-in", timeout=30)
    assert checkin.status_code == 200, checkin.text

    row = _get_walker_user(walker_user_id)
    monitoring = row.get("quality_monitoring") or {}
    assert int(monitoring.get("completed_walks", 0)) == 0
    assert int(monitoring.get("reset_count", 0)) >= 1


def test_restricted_daily_limit_blocks_third_walk_creation(base_url: str, quality_env: dict[str, Any]):
    client = quality_env["client"]
    now = datetime.now(timezone.utc)

    mongo_client, users = _mongo_collection("users")
    try:
        seeded_walker = users.find_one({"email": "walker@petpasso.com", "role": "passeador"}, {"_id": 0})
        if not seeded_walker:
            pytest.skip("Passeador seed walker@petpasso.com não encontrado")
        walker_user_id = str(seeded_walker.get("id") or "")
        walker_name = str(seeded_walker.get("full_name") or "Passeador")
        walker_partner_id = f"partner-{walker_user_id}"
    finally:
        mongo_client.close()

    target_date = (now + timedelta(days=2)).strftime("%Y-%m-%d")
    first_dt = datetime.strptime(f"{target_date} 09:00", "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    second_dt = datetime.strptime(f"{target_date} 11:00", "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)

    _replace_walker_walks(
        walker_user_id,
        [
            _walk_document(
                walker_user_id=walker_user_id,
                walker_name=walker_name,
                status="Agendado",
                dt=first_dt,
                walk_suffix="LIMIT_1",
            ),
            _walk_document(
                walker_user_id=walker_user_id,
                walker_name=walker_name,
                status="Indo buscar o pet",
                dt=second_dt,
                walk_suffix="LIMIT_2",
            ),
        ],
    )

    mongo_client, users = _mongo_collection("users")
    try:
        users.update_one(
            {"id": walker_user_id},
            {
                "$set": {
                    "quality_status": "restrito",
                    "quality_monitoring": {
                        "active": True,
                        "severity": "padrao",
                        "target_walks": 7,
                        "completed_walks": 0,
                        "reset_count": 0,
                        "severe_delay_incidents": 0,
                        "course_completed": False,
                        "quiz_passed": False,
                        "quiz_attempts": 0,
                        "consecutive_quiz_failures": 0,
                        "review_recommended": False,
                    },
                }
            },
        )
    finally:
        mongo_client.close()

    pets_resp = client.get(f"{base_url}/api/pets", timeout=30)
    assert pets_resp.status_code == 200, pets_resp.text
    pets = pets_resp.json()
    if not pets:
        pytest.skip("Sem pet na conta cliente para validar criação de passeio")

    payload = {
        "pet_name": pets[0]["pet_name"],
        "pet_id": pets[0]["id"],
        "client_name": "TEST_ITER31_CLIENT",
        "walk_date": target_date,
        "walk_time": "13:00",
        "duration_minutes": 30,
        "walk_type": "Individual",
        "walker_id": walker_partner_id,
        "pickup_street": "Rua Teste 31",
        "pickup_number": "100",
        "pickup_neighborhood": "Pituba",
        "pickup_complement": "",
        "location_reference": "TEST_ITER31",
        "pet_behavior_notes": "",
        "notes": "TEST_ITER31 limit",
    }
    create_resp = client.post(f"{base_url}/api/walks", json=payload, timeout=30)
    assert create_resp.status_code == 400
    assert "limite diário" in create_resp.text.lower()


def test_client_walkers_endpoint_hides_negative_status_and_preserves_public_rating(base_url: str, quality_env: dict[str, Any]):
    walker_user_id = quality_env["walker_user_id"]
    client = quality_env["client"]

    mongo_client, users = _mongo_collection("users")
    try:
        users.update_one(
            {"id": walker_user_id},
            {"$set": {"quality_status": "suspenso", "updated_at": datetime.now(timezone.utc).isoformat()}},
        )
    finally:
        mongo_client.close()

    response = client.get(f"{base_url}/api/walkers", timeout=30)
    assert response.status_code == 200, response.text
    walkers = response.json()
    assert isinstance(walkers, list)
    assert all(item.get("quality_status") not in {"restrito", "suspenso", "em_observacao"} for item in walkers)
    if walkers:
        assert "rating_avg" in walkers[0]
        assert "public_rating_label" in walkers[0]


def test_client_walkers_endpoint_respects_priority_sorting(base_url: str, quality_env: dict[str, Any]):
    client = quality_env["client"]
    response = client.get(f"{base_url}/api/walkers", timeout=30)
    assert response.status_code == 200, response.text
    walkers = response.json()
    assert isinstance(walkers, list)
    if len(walkers) < 2:
        pytest.skip("Poucos passeadores visíveis para validar ordenação")

    status_rank = {"ativo_premium": 4, "ativo": 3, "em_observacao": 2, "restrito": 1, "suspenso": 0}

    def key(row: dict[str, Any]):
        return (
            -status_rank.get(str(row.get("quality_status") or "ativo"), 0),
            -float(row.get("rating_avg") or 0),
            -float(row.get("rating_recent_avg") or 0),
            -int(row.get("rating_count") or 0),
            float(row.get("severe_delay_rate") or 0),
            float(row.get("no_show_rate") or 0),
            -int(row.get("completed_walks") or 0),
        )

    assert walkers == sorted(walkers, key=key)
