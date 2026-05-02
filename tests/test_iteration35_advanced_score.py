from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from statistics import pstdev
from typing import Any

import bcrypt
import pytest
import requests
from dotenv import dotenv_values
from pymongo import MongoClient


# Module coverage: score avançado (40/25/20/15 + recência 50/30/20 + consistência + penalidade severa + fallback + ranking)
# Plus auth playbook checks (cookies, bcrypt, lockout, CORS)

CLIENT_CREDS = {"email": "cliente@petpasso.com", "password": "Cliente@123"}
ADMIN_CREDS = {"email": "admin@petpasso.com", "password": "Admin@123"}


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


def _create_test_walker(db, slug: str) -> dict[str, str]:
    email = f"test_iter35_{slug}@petpasso.com"
    full_name = f"TEST_ITER35 {slug}"
    now_iso = datetime.now(timezone.utc).isoformat()

    existing = db.users.find_one({"email": email}, {"_id": 0, "id": 1})
    walker_id = existing["id"] if existing else str(uuid.uuid4())

    payload = {
        "id": walker_id,
        "full_name": full_name,
        "email": email,
        "password_hash": bcrypt.hashpw("TestIter35@123".encode("utf-8"), bcrypt.gensalt()).decode("utf-8"),
        "role": "passeador",
        "isAdmin": False,
        "isActive": True,
        "permissions": {},
        "region": "TEST_BAIRRO",
        "quality_status": "ativo",
        "quality_status_reason": "TEST_ITER35 baseline",
        "availability_days": ["seg", "ter", "qua", "qui", "sex", "sab", "dom"],
        "availability_start_time": "08:00",
        "availability_end_time": "18:00",
        "availability_blocks": [],
        "unavailable_until": None,
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
        "created_at": now_iso,
        "updated_at": now_iso,
        "last_active_at": now_iso,
    }
    db.users.update_one({"id": walker_id}, {"$set": payload}, upsert=True)
    return {"id": walker_id, "name": full_name, "email": email, "partner_id": f"partner-{walker_id}"}


def _walk_doc(
    *,
    walker: dict[str, str],
    walk_dt: datetime,
    status: str = "Finalizado",
    rating: int | None = None,
    client_user_id: str | None = None,
    client_name: str = "TEST_ITER35_CLIENT",
    severe_delay: bool = False,
    note_prefix: str = "TEST_ITER35",
) -> dict[str, Any]:
    now_iso = datetime.now(timezone.utc).isoformat()
    check_in_at = None
    if severe_delay:
        check_in_at = (walk_dt + timedelta(minutes=20)).isoformat()

    return {
        "id": f"{note_prefix}_{uuid.uuid4().hex[:10]}",
        "pet_name": "TEST_ITER35_PET",
        "pet_ids": [],
        "shared_pet_names": [],
        "shared_client_names": [],
        "shared_owner_keys": [],
        "participant_user_ids": [],
        "client_user_id": client_user_id,
        "client_name": client_name,
        "walk_type": "Individual",
        "shared_context": None,
        "shared_approved": False,
        "shared_group": None,
        "walk_date": walk_dt.strftime("%Y-%m-%d"),
        "walk_time": walk_dt.strftime("%H:%M"),
        "duration_minutes": 30,
        "walker_id": walker["partner_id"],
        "walker_user_id": walker["id"],
        "walker_name": walker["name"],
        "pickup_street": "Rua Teste",
        "pickup_number": "35",
        "pickup_neighborhood": "TEST_BAIRRO",
        "pickup_complement": "",
        "location_reference": "TEST_ITER35",
        "security_code": "1234",
        "did_pee": False,
        "did_poop": False,
        "rating": rating,
        "rating_comment": "",
        "summary_text": "",
        "pet_behavior_notes": "",
        "notes": f"{note_prefix} auto",
        "motivoCancelamento": "",
        "tipoCancelamento": None,
        "penalidadePercentual": 0,
        "base_price": 45.0,
        "walker_payout": 33.75,
        "scheduled_start_at": walk_dt.isoformat(),
        "walker_check_in_at": check_in_at,
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
        "walk_datetime_iso": walk_dt.isoformat(),
        "created_at": now_iso,
        "updated_at": now_iso,
    }


def _replace_walker_walks(db, walker_user_id: str, walks: list[dict[str, Any]]):
    db.walks.delete_many({"walker_user_id": walker_user_id, "notes": {"$regex": r"^TEST_ITER35"}})
    if walks:
        db.walks.insert_many(walks)


def _get_quality(base_url: str, walker_email: str) -> dict[str, Any]:
    session = _login(base_url, walker_email, "TestIter35@123")
    try:
        response = session.get(f"{base_url}/api/walker/quality", timeout=30)
        assert response.status_code == 200, response.text
        return response.json()
    finally:
        session.close()


def _base_score_from_formula(rating_weighted_avg: float, completion_rate: float, punctuality_rate: float, no_show_reliability: float) -> float:
    rating_norm = max(0.0, min(1.0, rating_weighted_avg / 5.0))
    raw = ((rating_norm * 0.40) + (completion_rate * 0.25) + (punctuality_rate * 0.20) + (no_show_reliability * 0.15)) * 100
    return max(0.0, min(100.0, raw))


@pytest.fixture(scope="module")
def score_env(base_url: str):
    mongo_client, db = _mongo_db()
    created_ids: list[str] = []
    try:
        db.walks.delete_many({"notes": {"$regex": r"^TEST_ITER35"}})
        db.users.delete_many({"email": {"$regex": r"^test_iter35_.*@petpasso\.com$"}})

        walkers = {
            "formula": _create_test_walker(db, "formula"),
            "recency": _create_test_walker(db, "recency"),
            "consistent": _create_test_walker(db, "consistent"),
            "volatile": _create_test_walker(db, "volatile"),
            "penalized": _create_test_walker(db, "penalized"),
            "loyalty": _create_test_walker(db, "loyalty"),
            "newbie": _create_test_walker(db, "newbie"),
            "rank_hi": _create_test_walker(db, "rank_hi"),
            "rank_low": _create_test_walker(db, "rank_low"),
            "rank_tie_near": _create_test_walker(db, "rank_tie_near"),
            "rank_tie_far": _create_test_walker(db, "rank_tie_far"),
        }
        created_ids.extend([item["id"] for item in walkers.values()])

        yield {"db": db, "walkers": walkers, "base_url": base_url}
    finally:
        db.walks.delete_many({"notes": {"$regex": r"^TEST_ITER35"}})
        if created_ids:
            db.users.delete_many({"id": {"$in": created_ids}})
        db.users.delete_many({"email": {"$regex": r"^test_iter35_.*@petpasso\.com$"}})
        mongo_client.close()


def test_score_base_uses_40_25_20_15_weights(score_env):
    db = score_env["db"]
    walker = score_env["walkers"]["formula"]
    now = datetime.now(timezone.utc)
    ratings = [5, 4, 5, 4, 5]

    walks = [
        _walk_doc(walker=walker, walk_dt=now - timedelta(days=i + 1), status="Finalizado", rating=rating)
        for i, rating in enumerate(ratings)
    ]
    _replace_walker_walks(db, walker["id"], walks)

    quality = _get_quality(score_env["base_url"], walker["email"])
    expected_score_base = _base_score_from_formula(
        rating_weighted_avg=quality["rating_weighted_avg"],
        completion_rate=1.0,
        punctuality_rate=1.0,
        no_show_reliability=1.0,
    )
    assert quality["score_base"] == pytest.approx(expected_score_base, abs=0.01)


def test_recency_factor_respects_50_30_20_blend(score_env):
    db = score_env["db"]
    walker = score_env["walkers"]["recency"]
    now = datetime.now(timezone.utc)

    walks: list[dict[str, Any]] = []
    for day_offset in [1, 2, 3, 4, 5]:
        walks.append(_walk_doc(walker=walker, walk_dt=now - timedelta(days=day_offset), status="Finalizado", rating=2))
    for day_offset in [10, 12, 15, 18, 22]:
        walks.append(_walk_doc(walker=walker, walk_dt=now - timedelta(days=day_offset), status="Finalizado", rating=3))
    for day_offset in [35, 40, 45, 50, 55]:
        walks.append(_walk_doc(walker=walker, walk_dt=now - timedelta(days=day_offset), status="Finalizado", rating=5))
    _replace_walker_walks(db, walker["id"], walks)

    quality = _get_quality(score_env["base_url"], walker["email"])

    def subset_base(ratings_subset: list[int]) -> float:
        if len(ratings_subset) < 5:
            return 75.0
        rating_avg = round(sum(ratings_subset) / len(ratings_subset), 2)
        recent = ratings_subset[:10]
        recent_avg = round(sum(recent) / len(recent), 2)
        rating_weighted = round((recent_avg * 0.7) + (rating_avg * 0.3), 1)
        return _base_score_from_formula(rating_weighted, 1.0, 1.0, 1.0)

    all_ratings = [2, 2, 2, 2, 2, 3, 3, 3, 3, 3, 5, 5, 5, 5, 5]
    ratings_30 = [2, 2, 2, 2, 2, 3, 3, 3, 3, 3]
    ratings_7 = [2, 2, 2, 2, 2]

    expected_reference = subset_base(all_ratings)
    expected_30 = subset_base(ratings_30)
    expected_7 = subset_base(ratings_7)
    expected_blended = (expected_7 * 0.5) + (expected_30 * 0.3) + (expected_reference * 0.2)
    expected_factor = max(0.75, min(1.25, expected_blended / expected_reference))

    assert quality["recency_factor"] == pytest.approx(expected_factor, abs=0.02)


def test_consistency_factor_decreases_with_high_std_dev(score_env):
    db = score_env["db"]
    now = datetime.now(timezone.utc)
    walker_consistent = score_env["walkers"]["consistent"]
    walker_volatile = score_env["walkers"]["volatile"]

    consistent_ratings = [4, 4, 4, 4, 4]
    volatile_ratings = [1, 5, 1, 5, 4]

    _replace_walker_walks(
        db,
        walker_consistent["id"],
        [_walk_doc(walker=walker_consistent, walk_dt=now - timedelta(days=i + 1), status="Finalizado", rating=r) for i, r in enumerate(consistent_ratings)],
    )
    _replace_walker_walks(
        db,
        walker_volatile["id"],
        [_walk_doc(walker=walker_volatile, walk_dt=now - timedelta(days=i + 1), status="Finalizado", rating=r) for i, r in enumerate(volatile_ratings)],
    )

    quality_consistent = _get_quality(score_env["base_url"], walker_consistent["email"])
    quality_volatile = _get_quality(score_env["base_url"], walker_volatile["email"])

    expected_consistent = max(0.85, min(1.08, 1.08 - (pstdev(consistent_ratings) * 0.12)))
    expected_volatile = max(0.85, min(1.08, 1.08 - (pstdev(volatile_ratings) * 0.12)))

    assert quality_consistent["consistency_factor"] == pytest.approx(expected_consistent, abs=0.02)
    assert quality_volatile["consistency_factor"] == pytest.approx(expected_volatile, abs=0.02)
    assert quality_volatile["consistency_factor"] < quality_consistent["consistency_factor"]


def test_severe_penalty_factor_reduces_score_on_no_show_and_severe_delay(score_env):
    db = score_env["db"]
    walker = score_env["walkers"]["penalized"]
    now = datetime.now(timezone.utc)

    walks = [
        _walk_doc(walker=walker, walk_dt=now - timedelta(days=1), status="Finalizado", rating=5),
        _walk_doc(walker=walker, walk_dt=now - timedelta(days=2), status="Finalizado", rating=5),
        _walk_doc(walker=walker, walk_dt=now - timedelta(days=3), status="Finalizado", rating=4, severe_delay=True),
        _walk_doc(walker=walker, walk_dt=now - timedelta(days=4), status="Não comparecimento do passeador", rating=None),
        _walk_doc(walker=walker, walk_dt=now - timedelta(days=5), status="Não comparecimento do passeador", rating=None),
        _walk_doc(walker=walker, walk_dt=now - timedelta(days=10), status="Finalizado", rating=5),
        _walk_doc(walker=walker, walk_dt=now - timedelta(days=12), status="Finalizado", rating=4),
    ]
    _replace_walker_walks(db, walker["id"], walks)

    quality = _get_quality(score_env["base_url"], walker["email"])
    assert quality["severe_penalty_factor"] < 1.0
    assert quality["score_final"] < quality["score_base"]


def test_loyalty_weight_1_2_for_clients_with_three_plus_walks(score_env):
    db = score_env["db"]
    walker = score_env["walkers"]["loyalty"]
    now = datetime.now(timezone.utc)

    recurring_client_id = "test-iter35-recurring-client"
    walks = [
        _walk_doc(walker=walker, walk_dt=now - timedelta(days=1), status="Finalizado", rating=5, client_user_id=recurring_client_id, client_name="Recorrente"),
        _walk_doc(walker=walker, walk_dt=now - timedelta(days=2), status="Finalizado", rating=5, client_user_id=recurring_client_id, client_name="Recorrente"),
        _walk_doc(walker=walker, walk_dt=now - timedelta(days=3), status="Finalizado", rating=5, client_user_id=recurring_client_id, client_name="Recorrente"),
        _walk_doc(walker=walker, walk_dt=now - timedelta(days=4), status="Finalizado", rating=1, client_user_id="u-unique-1", client_name="Único 1"),
        _walk_doc(walker=walker, walk_dt=now - timedelta(days=5), status="Finalizado", rating=1, client_user_id="u-unique-2", client_name="Único 2"),
    ]
    _replace_walker_walks(db, walker["id"], walks)

    quality = _get_quality(score_env["base_url"], walker["email"])
    unweighted = (5 + 5 + 5 + 1 + 1) / 5
    weighted_expected = ((5 * 1.2 * 3) + 1 + 1) / ((1.2 * 3) + 2)

    assert quality["rating_avg"] == pytest.approx(round(weighted_expected, 2), abs=0.02)
    assert quality["rating_avg"] > unweighted


def test_fallback_score_75_for_less_than_five_ratings(score_env):
    db = score_env["db"]
    walker = score_env["walkers"]["newbie"]
    now = datetime.now(timezone.utc)

    walks = [
        _walk_doc(walker=walker, walk_dt=now - timedelta(days=1), status="Finalizado", rating=1),
        _walk_doc(walker=walker, walk_dt=now - timedelta(days=2), status="Finalizado", rating=5),
        _walk_doc(walker=walker, walk_dt=now - timedelta(days=3), status="Finalizado", rating=2),
        _walk_doc(walker=walker, walk_dt=now - timedelta(days=4), status="Finalizado", rating=4),
    ]
    _replace_walker_walks(db, walker["id"], walks)

    quality = _get_quality(score_env["base_url"], walker["email"])
    assert quality["rating_count"] == 4
    assert quality["score_final"] == pytest.approx(75.0)
    assert quality["recency_factor"] == pytest.approx(1.0)
    assert quality["consistency_factor"] == pytest.approx(1.0)
    assert quality["severe_penalty_factor"] == pytest.approx(1.0)


def test_walker_list_ranking_prioritizes_score_then_proximity(score_env):
    db = score_env["db"]
    base_url = score_env["base_url"]
    client = _login(base_url, CLIENT_CREDS["email"], CLIENT_CREDS["password"])
    now = datetime.now(timezone.utc)
    target_date = (now + timedelta(days=1)).strftime("%Y-%m-%d")

    w_hi = score_env["walkers"]["rank_hi"]
    w_low = score_env["walkers"]["rank_low"]
    w_near = score_env["walkers"]["rank_tie_near"]
    w_far = score_env["walkers"]["rank_tie_far"]

    db.users.update_one({"id": w_hi["id"]}, {"$set": {"region": "BAIRRO_LONGE"}})
    db.users.update_one({"id": w_low["id"]}, {"$set": {"region": "TEST_BAIRRO"}})
    db.users.update_one({"id": w_near["id"]}, {"$set": {"region": "TEST_BAIRRO"}})
    db.users.update_one({"id": w_far["id"]}, {"$set": {"region": "OUTRA_REGIAO"}})

    _replace_walker_walks(db, w_hi["id"], [_walk_doc(walker=w_hi, walk_dt=now - timedelta(days=i + 1), status="Finalizado", rating=5) for i in range(6)])
    _replace_walker_walks(db, w_low["id"], [_walk_doc(walker=w_low, walk_dt=now - timedelta(days=i + 1), status="Finalizado", rating=3) for i in range(6)])
    _replace_walker_walks(db, w_near["id"], [_walk_doc(walker=w_near, walk_dt=now - timedelta(days=i + 1), status="Finalizado", rating=4) for i in range(4)])
    _replace_walker_walks(db, w_far["id"], [_walk_doc(walker=w_far, walk_dt=now - timedelta(days=i + 1), status="Finalizado", rating=4) for i in range(4)])

    response = client.get(
        f"{base_url}/api/walkers",
        params={"date": target_date, "duration_minutes": 30, "neighborhood": "test_bairro"},
        timeout=30,
    )
    assert response.status_code == 200, response.text
    rows = response.json()
    ids = [row["id"] for row in rows]

    # score dominante: rank_hi deve ficar na frente de rank_low mesmo com região pior
    assert ids.index(w_hi["partner_id"]) < ids.index(w_low["partner_id"])
    # empate por fallback (75): proximidade deve desempatar
    assert ids.index(w_near["partner_id"]) < ids.index(w_far["partner_id"])
    client.close()


def test_auth_login_sets_http_only_cookies(base_url: str):
    response = requests.post(
        f"{base_url}/api/auth/login",
        json=ADMIN_CREDS,
        timeout=20,
    )
    assert response.status_code == 200, response.text
    cookie_headers = ",".join(response.raw.headers.get_all("Set-Cookie") or [])
    assert "access_token=" in cookie_headers
    assert "refresh_token=" in cookie_headers
    assert "HttpOnly" in cookie_headers


def test_auth_bcrypt_hash_starts_with_2b():
    mongo_client, db = _mongo_db()
    try:
        admin = db.users.find_one({"email": ADMIN_CREDS["email"]}, {"_id": 0, "password_hash": 1})
        assert admin and isinstance(admin.get("password_hash"), str)
        assert admin["password_hash"].startswith("$2b$")
    finally:
        mongo_client.close()


def test_auth_bruteforce_lockout_after_five_failed_attempts(base_url: str):
    email = ADMIN_CREDS["email"]
    session = requests.Session()
    statuses = []
    for _ in range(6):
        response = session.post(
            f"{base_url}/api/auth/login",
            json={"email": email, "password": "WrongPassword!123"},
            timeout=20,
        )
        statuses.append(response.status_code)

    assert statuses.count(401) >= 5
    assert 429 in statuses

    mongo_client, db = _mongo_db()
    try:
        db.login_attempts.delete_many({"identifier": {"$regex": f":{email}$"}})
    finally:
        mongo_client.close()


def test_auth_cors_preflight_allows_credentials_and_explicit_origin(base_url: str):
    response = requests.options(
        f"{base_url}/api/auth/login",
        headers={
            "Origin": base_url,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
        timeout=20,
    )
    assert response.status_code in {200, 204}
    assert response.headers.get("Access-Control-Allow-Origin", "") == base_url
    assert response.headers.get("Access-Control-Allow-Credentials", "").lower() == "true"
