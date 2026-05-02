import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import bcrypt
import pytest
import requests
from dotenv import dotenv_values
from pymongo import MongoClient


# Module coverage: tip amount/duplication guards, score-impact anti-manipulation rules, admin review actions


if "/app/backend" not in sys.path:
    sys.path.append("/app/backend")
import server as backend_server  # type: ignore


def _mongo_db():
    backend_env = dotenv_values("/app/backend/.env")
    mongo_url = os.environ.get("MONGO_URL") or backend_env.get("MONGO_URL")
    db_name = os.environ.get("DB_NAME") or backend_env.get("DB_NAME")
    if not mongo_url or not db_name:
        pytest.skip("MONGO_URL/DB_NAME não configurados")

    client = MongoClient(str(mongo_url).strip().strip('"'))
    db = client[str(db_name).strip().strip('"')]
    return client, db


def _login_session(base_url: str, email: str, password: str) -> requests.Session:
    session = requests.Session()
    response = session.post(
        f"{base_url}/api/auth/login",
        json={"email": email, "password": password},
        timeout=25,
    )
    assert response.status_code == 200
    token = response.json().get("access_token")
    assert token
    session.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/json"})
    return session


def _create_and_finish_walk(api_client: requests.Session, base_url: str, suffix: str) -> dict:
    dt = datetime.now(timezone.utc) + timedelta(days=2)
    date_str = dt.strftime("%Y-%m-%d")
    slot_response = api_client.get(
        f"{base_url}/api/walkers/walker-1/availability-slots",
        params={"date": date_str, "duration_minutes": 30},
        timeout=25,
    )
    assert slot_response.status_code == 200
    slots = slot_response.json().get("available_slots", [])
    if not slots:
        pytest.skip("Sem horários disponíveis para walker-1")

    payload = {
        "pet_name": f"TEST_ITER37_Pet_{suffix}",
        "client_name": f"TEST_ITER37_Client_{suffix}",
        "walk_date": date_str,
        "walk_time": str(slots[0]),
        "duration_minutes": 30,
        "walk_type": "Individual",
        "walker_id": "walker-1",
        "pickup_street": "Rua Teste",
        "pickup_number": "100",
        "pickup_neighborhood": "Centro",
        "pickup_complement": "",
        "location_reference": "TEST_ITER37 referência",
        "notes": "TEST_ITER37",
    }
    created = api_client.post(f"{base_url}/api/walks", json=payload, timeout=25)
    assert created.status_code == 201
    walk = created.json()
    walk_id = walk["id"]

    pickup = api_client.patch(f"{base_url}/api/walks/{walk_id}/status", json={"status": "Indo buscar o pet"}, timeout=25)
    assert pickup.status_code == 200
    walking = api_client.patch(f"{base_url}/api/walks/{walk_id}/status", json={"status": "Passeando agora"}, timeout=25)
    assert walking.status_code == 200
    finished = api_client.patch(f"{base_url}/api/walks/{walk_id}/status", json={"status": "Finalizado"}, timeout=25)
    assert finished.status_code == 200

    persisted = api_client.get(f"{base_url}/api/walks/{walk_id}", timeout=25)
    assert persisted.status_code == 200
    return persisted.json()


def _mk_walk(
    *,
    idx: int,
    status: str = "Finalizado",
    rating: int = 5,
    days_ago: int = 1,
    client_user_id: str = "client-a",
    occurrence_status: str = "resolvido",
) -> dict:
    dt = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    return {
        "id": f"TEST_ITER37_WALK_{idx}",
        "status": status,
        "walk_datetime_iso": dt,
        "updated_at": dt,
        "rating": rating,
        "client_user_id": client_user_id,
        "client_name": f"Cliente {client_user_id}",
        "occurrence_status": occurrence_status,
        "base_price": 20.0,
        "charged_amount": 20.0,
    }


def _mk_tip(*, idx: int, walk_id: str, amount: float, client_user_id: str, paid_days_ago: int = 1) -> dict:
    paid_at = (datetime.now(timezone.utc) - timedelta(days=paid_days_ago)).isoformat()
    return {
        "id": f"TEST_ITER37_TIP_{idx}",
        "walk_id": walk_id,
        "client_user_id": client_user_id,
        "client_name": f"Cliente {client_user_id}",
        "amount": amount,
        "status": "paid",
        "paid_at": paid_at,
        "updated_at": paid_at,
        "created_at": paid_at,
        "suspicious_flag": False,
    }


@pytest.fixture()
def iter37_cleanup_scope():
    scope = {"walk_ids": [], "tip_ids": [], "session_ids": [], "user_ids": [], "emails": []}
    yield scope

    mongo_client, db = _mongo_db()
    try:
        if scope["walk_ids"]:
            db.tips.delete_many({"walk_id": {"$in": scope["walk_ids"]}})
            db.payment_transactions.delete_many({"walk_id": {"$in": scope["walk_ids"]}})
            db.walks.delete_many({"id": {"$in": scope["walk_ids"]}})
        if scope["tip_ids"]:
            db.tips.delete_many({"id": {"$in": scope["tip_ids"]}})
        if scope["session_ids"]:
            db.payment_transactions.delete_many({"session_id": {"$in": scope["session_ids"]}})
        if scope["user_ids"]:
            db.users.delete_many({"id": {"$in": scope["user_ids"]}})
        if scope["emails"]:
            for email in scope["emails"]:
                db.login_attempts.delete_many({"identifier": {"$regex": f":{email}$"}})
    finally:
        mongo_client.close()


def test_tip_checkout_enforces_minimum_and_dynamic_max(api_client, base_url, iter37_cleanup_scope):
    walk = _create_and_finish_walk(api_client, base_url, f"limits_{uuid.uuid4().hex[:6]}")
    iter37_cleanup_scope["walk_ids"].append(walk["id"])

    min_resp = api_client.post(
        f"{base_url}/api/walks/{walk['id']}/tips/checkout",
        json={"custom_amount": 0.5},
        timeout=25,
    )
    assert min_resp.status_code in (400, 422)
    assert "1" in min_resp.text

    max_allowed = round(min(50.0, float(walk.get("base_price") or walk.get("charged_amount") or 0.0) * 1.5), 2)
    over_limit = round(max_allowed + 0.01, 2)
    over_resp = api_client.post(
        f"{base_url}/api/walks/{walk['id']}/tips/checkout",
        json={"custom_amount": over_limit},
        timeout=25,
    )
    assert over_resp.status_code == 400
    assert "valor máximo permitido" in over_resp.text.lower()


def test_tip_checkout_blocks_duplicate_tip_on_same_walk(api_client, base_url, iter37_cleanup_scope):
    walk = _create_and_finish_walk(api_client, base_url, f"duplicate_{uuid.uuid4().hex[:6]}")
    iter37_cleanup_scope["walk_ids"].append(walk["id"])

    mongo_client, db = _mongo_db()
    tip_id = f"TEST_ITER37_DUP_TIP_{uuid.uuid4().hex[:8]}"
    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        db.tips.insert_one(
            {
                "id": tip_id,
                "walk_id": walk["id"],
                "client_user_id": "TEST_ITER37_CLIENT",
                "client_name": "TEST_ITER37_CLIENT",
                "walker_user_id": str(walk.get("walker_user_id") or ""),
                "walker_id": str(walk.get("walker_id") or "walker-1"),
                "walker_name": str(walk.get("walker_name") or "Passeador"),
                "amount": 10.0,
                "currency": "brl",
                "status": "paid",
                "payment_status": "paid",
                "checkout_session_id": f"cs_iter37_{uuid.uuid4().hex[:12]}",
                "checkout_url": "https://example.com/checkout",
                "tip_deadline_at": now_iso,
                "paid_at": now_iso,
                "suspicious_flag": False,
                "created_at": now_iso,
                "updated_at": now_iso,
            }
        )
        iter37_cleanup_scope["tip_ids"].append(tip_id)
    finally:
        mongo_client.close()

    response = api_client.post(
        f"{base_url}/api/walks/{walk['id']}/tips/checkout",
        json={"quick_amount": 5},
        timeout=25,
    )
    assert response.status_code == 400
    assert "já recebeu gorjeta" in response.text.lower()


def test_tip_impact_cap_is_never_above_10_percent_of_operational_score():
    walks = [_mk_walk(idx=i, rating=5, days_ago=(i % 10) + 1, client_user_id=f"client-{i % 5}") for i in range(1, 31)]
    tips = [_mk_tip(idx=i, walk_id=f"TEST_ITER37_WALK_{i}", amount=20.0, client_user_id=f"client-{i % 5}") for i in range(1, 31)]

    metrics = backend_server._compute_reputation_metrics(
        walker_walks=walks,
        quality_status="ativo",
        tip_total_amount=600.0,
        tip_rows=tips,
        walker_controls={"tip_score_impact_mode": "normal"},
        platform_tip_average=15.0,
    )

    assert float(metrics["tip_score_impact_points"]) <= float(metrics["tip_score_impact_cap_points"]) + 1e-9
    assert float(metrics["tip_score_impact_cap_points"]) <= float(metrics["score_operational_final"]) * 0.10 + 1e-9


def test_tip_window_uses_only_last_20_finished_walks():
    walks = [_mk_walk(idx=i, rating=5, days_ago=i, client_user_id=f"client-{i}") for i in range(1, 26)]
    tips = [_mk_tip(idx=i, walk_id=f"TEST_ITER37_WALK_{i}", amount=5.0, client_user_id=f"client-{i}", paid_days_ago=i) for i in range(1, 26)]

    metrics = backend_server._compute_tip_signal_metrics(
        walker_walks=walks,
        tip_rows=tips,
        rating_weighted_avg=4.8,
        severe_delay_rate=0.0,
        no_show_recent_7=0,
        status_penalty_factor=1.0,
        walker_controls={"tip_score_impact_mode": "normal"},
        platform_tip_average=5.0,
    )

    assert int(metrics["tip_recent_window_count"]) == 20


def test_tip_repetition_weight_100_70_40_applies_for_same_client():
    walks = [_mk_walk(idx=i, rating=5, days_ago=i, client_user_id="same-client") for i in range(1, 4)]
    tips = [
        _mk_tip(idx=1, walk_id="TEST_ITER37_WALK_1", amount=10.0, client_user_id="same-client", paid_days_ago=1),
        _mk_tip(idx=2, walk_id="TEST_ITER37_WALK_2", amount=5.0, client_user_id="same-client", paid_days_ago=2),
        _mk_tip(idx=3, walk_id="TEST_ITER37_WALK_3", amount=5.0, client_user_id="same-client", paid_days_ago=3),
    ]

    metrics = backend_server._compute_tip_signal_metrics(
        walker_walks=walks,
        tip_rows=tips,
        rating_weighted_avg=4.9,
        severe_delay_rate=0.0,
        no_show_recent_7=0,
        status_penalty_factor=1.0,
        walker_controls={"tip_score_impact_mode": "normal"},
        platform_tip_average=4.0,
    )

    # base_price=20 => max tip by walk is 30, then ratios are 10/30 and 5/30
    expected_ratio = ((10.0 / 30.0) * 1.0 + (5.0 / 30.0) * 0.7 + (5.0 / 30.0) * 0.4) / (1.0 + 0.7 + 0.4)
    assert pytest.approx(float(metrics["tip_weighted_ratio"]), rel=1e-3) == expected_ratio


def test_suspicious_tip_flag_auto_zeros_score_impact():
    walks = [_mk_walk(idx=i, rating=5, days_ago=i, client_user_id="repeat-client") for i in range(1, 6)]
    tips = [_mk_tip(idx=i, walk_id=f"TEST_ITER37_WALK_{i}", amount=30.0, client_user_id="repeat-client", paid_days_ago=i) for i in range(1, 6)]

    metrics = backend_server._compute_reputation_metrics(
        walker_walks=walks,
        quality_status="ativo",
        tip_total_amount=150.0,
        tip_rows=tips,
        walker_controls={"tip_score_impact_mode": "normal"},
        platform_tip_average=8.0,
    )

    assert bool(metrics["tip_suspicious_flag"]) is True
    assert float(metrics["tip_score_impact_points"]) == 0.0


@pytest.mark.parametrize(
    "quality_status,extra_walk,status_occ",
    [
        ("ativo", "Não comparecimento do passeador", "resolvido"),
        ("ativo", "Finalizado", "atraso_grave"),
        ("restrito", "Finalizado", "resolvido"),
    ],
)
def test_tip_never_compensates_no_show_severe_delay_or_restricted_status(quality_status, extra_walk, status_occ):
    base_walks = [_mk_walk(idx=i, rating=5, days_ago=i, client_user_id=f"client-{i}") for i in range(1, 8)]
    base_walks.append(
        _mk_walk(
            idx=99,
            status=extra_walk,
            rating=5,
            days_ago=1,
            client_user_id="client-risk",
            occurrence_status=status_occ,
        )
    )
    tips = [_mk_tip(idx=i, walk_id=f"TEST_ITER37_WALK_{i}", amount=10.0, client_user_id=f"client-{i}") for i in range(1, 8)]

    metrics = backend_server._compute_reputation_metrics(
        walker_walks=base_walks,
        quality_status=quality_status,
        tip_total_amount=70.0,
        tip_rows=tips,
        walker_controls={"tip_score_impact_mode": "normal"},
        platform_tip_average=5.0,
    )

    assert float(metrics["tip_score_impact_points"]) == 0.0


def test_admin_walker_performance_exposes_tip_fields(api_client, base_url):
    response = api_client.get(f"{base_url}/api/admin/walkers/performance", timeout=35)
    assert response.status_code == 200
    rows = response.json()
    assert isinstance(rows, list)
    if not rows:
        pytest.skip("Sem passeadores no ambiente")

    row = rows[0]
    for key in [
        "tip_suspicious_flag",
        "tip_origin_top_clients",
        "tip_score_impact_points",
        "tip_score_impact_mode",
    ]:
        assert key in row


def test_admin_tip_review_progressive_and_restore_flow(api_client, base_url, iter37_cleanup_scope):
    walker_id = f"TEST_ITER37_WALKER_{uuid.uuid4().hex[:8]}"
    walker_email = f"iter37_walker_{uuid.uuid4().hex[:8]}@petpasso.com"
    now_iso = datetime.now(timezone.utc).isoformat()

    mongo_client, db = _mongo_db()
    try:
        db.users.insert_one(
            {
                "id": walker_id,
                "full_name": "TEST ITER37 Walker",
                "email": walker_email,
                "password_hash": bcrypt.hashpw("Passeador@123".encode("utf-8"), bcrypt.gensalt()).decode("utf-8"),
                "role": "passeador",
                "isAdmin": False,
                "isActive": True,
                "region": "TEST",
                "created_at": now_iso,
                "updated_at": now_iso,
            }
        )
        iter37_cleanup_scope["user_ids"].append(walker_id)
    finally:
        mongo_client.close()

    for expected_mode in ["ignore_current", "ignore_recent_window", "blocked_until_review"]:
        action_response = api_client.post(
            f"{base_url}/api/admin/walkers/{walker_id}/action",
            json={"action": "tip_review_progressive", "note": "TEST_ITER37 revisão progressiva"},
            timeout=35,
        )
        assert action_response.status_code == 200
        body = action_response.json()
        assert body["tip_score_impact_mode"] == expected_mode

    restore_response = api_client.post(
        f"{base_url}/api/admin/walkers/{walker_id}/action",
        json={"action": "tip_restore_impact", "note": "TEST_ITER37 restaurar"},
        timeout=35,
    )
    assert restore_response.status_code == 200
    assert restore_response.json()["tip_score_impact_mode"] == "normal"


def test_auth_playbook_bcrypt_cookie_cors_and_lockout(base_url, iter37_cleanup_scope):
    mongo_client, db = _mongo_db()
    try:
        seeded_admin = db.users.find_one({"email": "admin@petpasso.com"}, {"password_hash": 1})
        assert seeded_admin is not None
        assert str(seeded_admin.get("password_hash", "")).startswith("$2b$")
    finally:
        mongo_client.close()

    login_response = requests.post(
        f"{base_url}/api/auth/login",
        json={"email": "cliente@petpasso.com", "password": "Cliente@123"},
        timeout=25,
    )
    assert login_response.status_code == 200
    set_cookie = login_response.headers.get("set-cookie", "").lower()
    assert "access_token=" in set_cookie
    assert "refresh_token=" in set_cookie
    assert "httponly" in set_cookie

    origin = base_url.rstrip("/")
    cors_response = requests.options(
        f"{base_url}/api/auth/login",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
        timeout=25,
    )
    assert cors_response.status_code in (200, 204)
    assert cors_response.headers.get("access-control-allow-credentials") == "true"
    assert cors_response.headers.get("access-control-allow-origin") == origin

    attacker_email = f"iter37_lockout_{uuid.uuid4().hex[:8]}@petpasso.com"
    iter37_cleanup_scope["emails"].append(attacker_email)
    for _ in range(5):
        response = requests.post(
            f"{base_url}/api/auth/login",
            json={"email": attacker_email, "password": "wrong-password"},
            timeout=25,
        )
        assert response.status_code in (401, 429)

    blocked = requests.post(
        f"{base_url}/api/auth/login",
        json={"email": attacker_email, "password": "wrong-password"},
        timeout=25,
    )
    assert blocked.status_code == 429
