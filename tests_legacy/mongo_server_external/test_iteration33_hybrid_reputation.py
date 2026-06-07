from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
import pytest
import requests
from dotenv import dotenv_values
from pymongo import MongoClient


# Module coverage: reputação híbrida 70/30 + gatilhos críticos + ranking + exibição pública + auth playbook

ADMIN_CREDS = {"email": "admin@petpasso.com", "password": "Admin@123"}
CLIENT_CREDS = {"email": "cliente@petpasso.com", "password": "Cliente@123"}


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
    status: str = "Finalizado",
    rating: int | None = None,
    note_prefix: str = "TEST_ITER33",
    client_user_id: str | None = None,
    client_name: str = "TEST_ITER33_CLIENT",
    suspected_disintermediation: bool = False,
) -> dict[str, Any]:
    dt = datetime.strptime(f"{walk_date} {walk_time}", "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    now_iso = datetime.now(timezone.utc).isoformat()
    return {
        "id": f"{note_prefix}_{uuid.uuid4().hex[:10]}",
        "pet_name": "TEST_ITER33_PET",
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
        "walk_date": walk_date,
        "walk_time": walk_time,
        "duration_minutes": 30,
        "walker_id": f"partner-{walker_user_id}",
        "walker_user_id": walker_user_id,
        "walker_name": walker_name,
        "pickup_street": "Rua Teste",
        "pickup_number": "33",
        "pickup_neighborhood": "TEST_BAIRRO",
        "pickup_complement": "",
        "location_reference": "TEST_ITER33",
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
        "suspected_disintermediation": suspected_disintermediation,
    }


def _upsert_test_walker(db, *, slug: str) -> dict[str, str]:
    email = f"test_iter33_{slug}@petpasso.com"
    full_name = f"TEST_ITER33 {slug}"
    now_iso = datetime.now(timezone.utc).isoformat()
    existing = db.users.find_one({"email": email}, {"_id": 0, "id": 1, "full_name": 1})
    payload = {
        "full_name": full_name,
        "email": email,
        "role": "passeador",
        "isAdmin": False,
        "isActive": True,
        "password_hash": bcrypt.hashpw("TestIter33@123".encode("utf-8"), bcrypt.gensalt()).decode("utf-8"),
        "region": "TEST_BAIRRO",
        "availability_days": ["seg", "ter", "qua", "qui", "sex"],
        "availability_start_time": "08:00",
        "availability_end_time": "18:00",
        "availability_blocks": [],
        "unavailable_until": None,
        "quality_status": "ativo",
        "quality_status_reason": "TEST_ITER33 baseline",
        "premium_override": False,
        "updated_at": now_iso,
    }

    if existing:
        db.users.update_one({"id": existing["id"]}, {"$set": payload})
        return {"id": existing["id"], "name": full_name, "email": email}

    walker_id = str(uuid.uuid4())
    db.users.insert_one(
        {
            "id": walker_id,
            "created_at": now_iso,
            "last_active_at": now_iso,
            "permissions": {},
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
    return {"id": walker_id, "name": full_name, "email": email}


def _replace_walker_walks(db, walker_user_id: str, walks: list[dict[str, Any]]):
    db.walks.delete_many({"walker_user_id": walker_user_id, "notes": {"$regex": r"^TEST_ITER33"}})
    if walks:
        db.walks.insert_many(walks)


def _admin_recalc(admin_session: requests.Session, base_url: str):
    response = admin_session.get(f"{base_url}/api/admin/walkers/performance", timeout=30)
    assert response.status_code == 200, response.text


def _get_walker_user(db, walker_user_id: str) -> dict[str, Any]:
    row = db.users.find_one({"id": walker_user_id}, {"_id": 0})
    assert row is not None
    return row


def _weekday_date(base: datetime, weekday: int = 0) -> str:
    # weekday: 0=segunda
    days_ahead = (weekday - base.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    return (base + timedelta(days=days_ahead)).strftime("%Y-%m-%d")


@pytest.fixture(scope="module")
def hybrid_env(base_url: str):
    mongo_client, db = _mongo_db()
    users = db.users
    walks = db.walks
    created_ids: list[str] = []
    try:
        users.delete_many({"email": {"$regex": r"^test_iter33_.*@petpasso\.com$"}})
        walks.delete_many({"notes": {"$regex": r"^TEST_ITER33"}})

        walker_calc = _upsert_test_walker(db, slug="calc")
        walker_low = _upsert_test_walker(db, slug="under5")
        walker_crit = _upsert_test_walker(db, slug="critical")
        walker_a = _upsert_test_walker(db, slug="rank_a")
        walker_b = _upsert_test_walker(db, slug="rank_b")
        walker_c = _upsert_test_walker(db, slug="rank_c")
        walker_d = _upsert_test_walker(db, slug="rank_d")
        walker_auto = _upsert_test_walker(db, slug="autoupdate")

        for walker in [walker_calc, walker_low, walker_crit, walker_a, walker_b, walker_c, walker_d, walker_auto]:
            created_ids.append(walker["id"])

        admin_session = _login(base_url, ADMIN_CREDS["email"], ADMIN_CREDS["password"])
        client_session = _login(base_url, CLIENT_CREDS["email"], CLIENT_CREDS["password"])
        client_me = client_session.get(f"{base_url}/api/auth/me", timeout=20)
        assert client_me.status_code == 200, client_me.text
        client_user = client_me.json()

        walker_auto_session = _login(base_url, walker_auto["email"], "TestIter33@123")

        yield {
            "db": db,
            "base_url": base_url,
            "admin": admin_session,
            "client": client_session,
            "client_user": client_user,
            "walker_auto_session": walker_auto_session,
            "walkers": {
                "calc": walker_calc,
                "under5": walker_low,
                "critical": walker_crit,
                "rank_a": walker_a,
                "rank_b": walker_b,
                "rank_c": walker_c,
                "rank_d": walker_d,
                "autoupdate": walker_auto,
            },
        }

        admin_session.close()
        client_session.close()
        walker_auto_session.close()
    finally:
        try:
            walks.delete_many({"notes": {"$regex": r"^TEST_ITER33"}})
            if created_ids:
                users.delete_many({"id": {"$in": created_ids}})
            users.delete_many({"email": {"$regex": r"^test_iter33_.*@petpasso\.com$"}})
        finally:
            mongo_client.close()


def test_auth_bcrypt_seed_hash_uses_2b_prefix():
    mongo_client, db = _mongo_db()
    try:
        admin = db.users.find_one({"email": "admin@petpasso.com"}, {"_id": 0, "password_hash": 1})
        assert admin is not None
        assert str(admin.get("password_hash") or "").startswith("$2b$")
    finally:
        mongo_client.close()


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


def test_seed_admin_login_works_with_current_seed_credentials(base_url: str):
    response = requests.post(
        f"{base_url}/api/auth/login",
        json=ADMIN_CREDS,
        timeout=20,
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload.get("user", {}).get("email") == ADMIN_CREDS["email"]


def test_weighted_rating_uses_70_30_and_rounds_to_one_decimal(hybrid_env: dict[str, Any]):
    db = hybrid_env["db"]
    walker = hybrid_env["walkers"]["calc"]
    admin = hybrid_env["admin"]
    base_url = hybrid_env["base_url"]

    start = datetime.now(timezone.utc)
    ratings = [5, 5, 5, 4, 4, 4, 4, 4, 4, 4, 1, 1]
    walks = [
        _walk_doc(
            walker_user_id=walker["id"],
            walker_name=walker["name"],
            walk_date=(start - timedelta(days=index + 1)).strftime("%Y-%m-%d"),
            walk_time="09:00",
            rating=rating,
        )
        for index, rating in enumerate(ratings)
    ]
    _replace_walker_walks(db, walker["id"], walks)
    _admin_recalc(admin, base_url)

    row = _get_walker_user(db, walker["id"])
    metrics = row.get("quality_metrics") or {}
    assert metrics.get("rating_recent_avg") == 4.3
    assert metrics.get("rating_avg") == 3.75
    assert metrics.get("rating_weighted_avg") == 4.1


def test_under_five_ratings_allows_only_light_observation_not_restrict_or_suspend(hybrid_env: dict[str, Any]):
    db = hybrid_env["db"]
    walker = hybrid_env["walkers"]["under5"]
    admin = hybrid_env["admin"]
    base_url = hybrid_env["base_url"]

    start = datetime.now(timezone.utc)
    walks = [
        _walk_doc(
            walker_user_id=walker["id"],
            walker_name=walker["name"],
            walk_date=(start - timedelta(days=index + 1)).strftime("%Y-%m-%d"),
            walk_time="10:00",
            rating=1,
        )
        for index in range(4)
    ]
    _replace_walker_walks(db, walker["id"], walks)
    _admin_recalc(admin, base_url)

    row = _get_walker_user(db, walker["id"])
    assert int((row.get("quality_metrics") or {}).get("rating_count", 0)) == 4
    assert row.get("quality_status") in {"ativo", "em_observacao"}
    assert row.get("walker_operational_status") in {"ativo", "observacao"}


def test_status_uses_weighted_rating_when_five_or_more_reviews(hybrid_env: dict[str, Any]):
    db = hybrid_env["db"]
    walker = hybrid_env["walkers"]["calc"]
    admin = hybrid_env["admin"]
    base_url = hybrid_env["base_url"]

    start = datetime.now(timezone.utc)
    ratings = [4, 4, 4, 3, 3]
    walks = [
        _walk_doc(
            walker_user_id=walker["id"],
            walker_name=walker["name"],
            walk_date=(start - timedelta(days=index + 1)).strftime("%Y-%m-%d"),
            walk_time="09:30",
            rating=rating,
        )
        for index, rating in enumerate(ratings)
    ]
    _replace_walker_walks(db, walker["id"], walks)
    _admin_recalc(admin, base_url)

    row = _get_walker_user(db, walker["id"])
    metrics = row.get("quality_metrics") or {}
    assert int(metrics.get("rating_count", 0) or 0) >= 5
    assert float(metrics.get("rating_weighted_avg", 0.0) or 0.0) < 3.8
    assert row.get("quality_status") == "restrito"


def test_critical_triggers_override_rating_and_apply_negative_status(hybrid_env: dict[str, Any]):
    db = hybrid_env["db"]
    walker = hybrid_env["walkers"]["critical"]
    admin = hybrid_env["admin"]
    base_url = hybrid_env["base_url"]

    now = datetime.now(timezone.utc)
    baseline = [
        _walk_doc(
            walker_user_id=walker["id"],
            walker_name=walker["name"],
            walk_date=(now - timedelta(days=index + 2)).strftime("%Y-%m-%d"),
            walk_time="11:00",
            rating=5,
        )
        for index in range(6)
    ]

    with_no_show = baseline + [
        _walk_doc(
            walker_user_id=walker["id"],
            walker_name=walker["name"],
            walk_date=(now - timedelta(days=1)).strftime("%Y-%m-%d"),
            walk_time="11:30",
            status="Não comparecimento do passeador",
            rating=None,
        )
    ]
    _replace_walker_walks(db, walker["id"], with_no_show)
    _admin_recalc(admin, base_url)
    row_no_show = _get_walker_user(db, walker["id"])
    assert row_no_show.get("quality_status") in {"restrito", "suspenso"}

    with_fraud = baseline + [
        _walk_doc(
            walker_user_id=walker["id"],
            walker_name=walker["name"],
            walk_date=now.strftime("%Y-%m-%d"),
            walk_time="12:00",
            status="Finalizado",
            rating=5,
            suspected_disintermediation=True,
        )
    ]
    _replace_walker_walks(db, walker["id"], with_fraud)
    _admin_recalc(admin, base_url)
    row_fraud = _get_walker_user(db, walker["id"])
    assert row_fraud.get("quality_status") == "suspenso"


def test_ranking_prioritizes_weighted_then_general_rating(hybrid_env: dict[str, Any]):
    db = hybrid_env["db"]
    admin = hybrid_env["admin"]
    client = hybrid_env["client"]
    base_url = hybrid_env["base_url"]
    walkers = hybrid_env["walkers"]

    now = datetime.now(timezone.utc)
    date_query = _weekday_date(now, weekday=0)

    # rank_a: weighted higher, average lower (deve vir antes de rank_b)
    ratings_a = [5, 5, 5, 5, 5, 4, 4, 4, 4, 4, 1, 1]
    # rank_b: weighted lower, average higher
    ratings_b = [5, 4, 4, 4, 4, 4, 4, 4, 4, 4, 5, 5]
    # rank_c e rank_d: weighted arredondado igual (4.1), desempate por average (rank_c > rank_d)
    ratings_c = [4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 5, 5]
    ratings_d = [5, 5, 4, 4, 4, 4, 4, 4, 4, 4, 3, 2]

    for slug, ratings in {
        "rank_a": ratings_a,
        "rank_b": ratings_b,
        "rank_c": ratings_c,
        "rank_d": ratings_d,
    }.items():
        walker = walkers[slug]
        rows = [
            _walk_doc(
                walker_user_id=walker["id"],
                walker_name=walker["name"],
                walk_date=(now - timedelta(days=index + 1)).strftime("%Y-%m-%d"),
                walk_time="09:00",
                rating=rating,
            )
            for index, rating in enumerate(ratings)
        ]
        _replace_walker_walks(db, walker["id"], rows)

    _admin_recalc(admin, base_url)

    response = client.get(
        f"{base_url}/api/walkers",
        params={
            "date": date_query,
            "duration_minutes": 30,
            "preferred_time": "09:00",
            "neighborhood": "TEST_BAIRRO",
        },
        timeout=30,
    )
    assert response.status_code == 200, response.text
    payload = response.json()

    names = [row.get("name") for row in payload]
    idx_a = names.index(walkers["rank_a"]["name"])
    idx_b = names.index(walkers["rank_b"]["name"])
    idx_c = names.index(walkers["rank_c"]["name"])
    idx_d = names.index(walkers["rank_d"]["name"])

    assert idx_a < idx_b
    assert idx_c < idx_d


def test_public_rating_visibility_threshold_and_no_recent_weighted_exposure(hybrid_env: dict[str, Any]):
    db = hybrid_env["db"]
    admin = hybrid_env["admin"]
    client = hybrid_env["client"]
    base_url = hybrid_env["base_url"]
    walkers = hybrid_env["walkers"]

    now = datetime.now(timezone.utc)
    date_query = _weekday_date(now, weekday=0)

    calc_rows = [
        _walk_doc(
            walker_user_id=walkers["calc"]["id"],
            walker_name=walkers["calc"]["name"],
            walk_date=(now - timedelta(days=index + 1)).strftime("%Y-%m-%d"),
            walk_time="10:00",
            rating=5,
        )
        for index in range(5)
    ]
    under5_rows = [
        _walk_doc(
            walker_user_id=walkers["under5"]["id"],
            walker_name=walkers["under5"]["name"],
            walk_date=(now - timedelta(days=index + 1)).strftime("%Y-%m-%d"),
            walk_time="10:30",
            rating=4,
        )
        for index in range(4)
    ]
    _replace_walker_walks(db, walkers["calc"]["id"], calc_rows)
    _replace_walker_walks(db, walkers["under5"]["id"], under5_rows)
    _admin_recalc(admin, base_url)

    response = client.get(
        f"{base_url}/api/walkers",
        params={
            "date": date_query,
            "duration_minutes": 30,
            "preferred_time": "10:00",
            "neighborhood": "TEST_BAIRRO",
        },
        timeout=30,
    )
    assert response.status_code == 200, response.text

    rows = {item["name"]: item for item in response.json() if str(item.get("name", "")).startswith("TEST_ITER33")}
    with_5 = rows[walkers["calc"]["name"]]
    with_4 = rows[walkers["under5"]["name"]]

    assert with_5.get("public_rating_label") != "Novo na plataforma"
    assert with_4.get("public_rating_label") == "Novo na plataforma"

    assert "rating_recent_avg" not in with_5
    assert "rating_weighted_avg" not in with_5
    assert "rating_recent_avg" not in with_4
    assert "rating_weighted_avg" not in with_4


def test_recalculates_automatically_after_new_rating_submission(hybrid_env: dict[str, Any]):
    db = hybrid_env["db"]
    walker = hybrid_env["walkers"]["autoupdate"]
    client = hybrid_env["client"]
    client_user = hybrid_env["client_user"]
    walker_session = hybrid_env["walker_auto_session"]
    base_url = hybrid_env["base_url"]

    now = datetime.now(timezone.utc)
    old_walk = _walk_doc(
        walker_user_id=walker["id"],
        walker_name=walker["name"],
        walk_date=(now - timedelta(days=2)).strftime("%Y-%m-%d"),
        walk_time="09:00",
        rating=5,
        client_user_id=str(client_user.get("id") or ""),
        client_name=str(client_user.get("full_name") or "Cliente"),
    )
    unrated = _walk_doc(
        walker_user_id=walker["id"],
        walker_name=walker["name"],
        walk_date=(now - timedelta(days=1)).strftime("%Y-%m-%d"),
        walk_time="10:00",
        rating=None,
        client_user_id=str(client_user.get("id") or ""),
        client_name=str(client_user.get("full_name") or "Cliente"),
    )
    _replace_walker_walks(db, walker["id"], [old_walk, unrated])

    before_quality = walker_session.get(f"{base_url}/api/walker/quality", timeout=30)
    assert before_quality.status_code == 200, before_quality.text
    before_count = int(before_quality.json().get("rating_count", 0) or 0)

    rate_response = client.patch(
        f"{base_url}/api/walks/{unrated['id']}/rating",
        json={"rating": 1, "comment": "TEST_ITER33 recalculo"},
        timeout=30,
    )
    assert rate_response.status_code == 200, rate_response.text
    assert rate_response.json().get("rating") == 1

    after_quality = walker_session.get(f"{base_url}/api/walker/quality", timeout=30)
    assert after_quality.status_code == 200, after_quality.text
    after_data = after_quality.json()
    assert int(after_data.get("rating_count", 0) or 0) == max(2, before_count)
    assert float(after_data.get("rating_weighted_avg", 0.0) or 0.0) == 3.0
