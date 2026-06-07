import asyncio
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


# Module coverage: tips checkout/status/summary/admin audit + auth playbook (cookies/cors/lockout/bcrypt/seed_admin)


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
        pytest.skip("Sem horários disponíveis para walker-1 no ambiente de teste")

    payload = {
        "pet_name": f"TEST_ITER26_Pet_{suffix}",
        "client_name": f"TEST_ITER26_Client_{suffix}",
        "walk_date": date_str,
        "walk_time": str(slots[0]),
        "duration_minutes": 30,
        "walk_type": "Individual",
        "walker_id": "walker-1",
        "pickup_street": "Rua Teste",
        "pickup_number": "100",
        "pickup_neighborhood": "Centro",
        "pickup_complement": "",
        "location_reference": "TEST_ITER26 referência",
        "notes": "TEST_ITER26",
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


@pytest.fixture()
def tip_cleanup_scope():
    scope = {"walk_ids": [], "tip_ids": [], "sessions": []}
    yield scope

    mongo_client, db = _mongo_db()
    try:
        if scope["walk_ids"]:
            db.tips.delete_many({"walk_id": {"$in": scope["walk_ids"]}})
            db.payment_transactions.delete_many({"walk_id": {"$in": scope["walk_ids"]}})
            db.walks.delete_many({"id": {"$in": scope["walk_ids"]}})
        if scope["tip_ids"]:
            db.tips.delete_many({"id": {"$in": scope["tip_ids"]}})
        if scope["sessions"]:
            db.payment_transactions.delete_many({"session_id": {"$in": scope["sessions"]}})
    finally:
        mongo_client.close()


def test_tip_checkout_requires_finished_walk(api_client, base_url):
    suffix = uuid.uuid4().hex[:6]
    dt = datetime.now(timezone.utc) + timedelta(days=3)
    date_str = dt.strftime("%Y-%m-%d")
    slot_response = api_client.get(
        f"{base_url}/api/walkers/walker-1/availability-slots",
        params={"date": date_str, "duration_minutes": 30},
        timeout=25,
    )
    assert slot_response.status_code == 200
    slots = slot_response.json().get("available_slots", [])
    if not slots:
        pytest.skip("Sem horários disponíveis para validar passeio não finalizado")

    payload = {
        "pet_name": f"TEST_ITER26_NotFinished_{suffix}",
        "client_name": f"TEST_ITER26_Client_{suffix}",
        "walk_date": date_str,
        "walk_time": str(slots[0]),
        "duration_minutes": 30,
        "walk_type": "Individual",
        "walker_id": "walker-1",
        "pickup_street": "Rua Teste",
        "pickup_number": "101",
        "pickup_neighborhood": "Centro",
        "pickup_complement": "",
        "location_reference": "TEST_ITER26",
        "notes": "TEST_ITER26",
    }
    create_resp = api_client.post(f"{base_url}/api/walks", json=payload, timeout=25)
    assert create_resp.status_code == 201
    walk_id = create_resp.json()["id"]

    tip_resp = api_client.post(
        f"{base_url}/api/walks/{walk_id}/tips/checkout",
        json={"quick_amount": 5},
        timeout=25,
    )
    assert tip_resp.status_code == 400


def test_tip_checkout_rejects_expired_24h_window(api_client, base_url, tip_cleanup_scope):
    walk = _create_and_finish_walk(api_client, base_url, f"expired_{uuid.uuid4().hex[:6]}")
    tip_cleanup_scope["walk_ids"].append(walk["id"])

    mongo_client, db = _mongo_db()
    try:
        old_iso = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        db.walks.update_one(
            {"id": walk["id"]},
            {"$set": {"updated_at": old_iso, "decision_resolved_at": old_iso}},
        )
    finally:
        mongo_client.close()

    response = api_client.post(
        f"{base_url}/api/walks/{walk['id']}/tips/checkout",
        json={"quick_amount": 5},
        timeout=25,
    )
    assert response.status_code == 400


def test_tip_checkout_custom_amount_limit_validation(api_client, base_url, tip_cleanup_scope):
    walk = _create_and_finish_walk(api_client, base_url, f"limit_{uuid.uuid4().hex[:6]}")
    tip_cleanup_scope["walk_ids"].append(walk["id"])

    base_amount = float(walk.get("base_price") or walk.get("charged_amount") or 0)
    expected_max = round(min(50.0, base_amount * 1.5), 2)
    over_limit = round(expected_max + 0.01, 2)
    if over_limit <= 0:
        pytest.skip("Não foi possível obter valor base válido para validar limite de gorjeta")

    response = api_client.post(
        f"{base_url}/api/walks/{walk['id']}/tips/checkout",
        json={"custom_amount": over_limit},
        timeout=25,
    )
    assert response.status_code == 400


def test_tip_checkout_creates_stripe_session_for_finished_walk(api_client, base_url, tip_cleanup_scope):
    walk = _create_and_finish_walk(api_client, base_url, f"checkout_{uuid.uuid4().hex[:6]}")
    tip_cleanup_scope["walk_ids"].append(walk["id"])

    response = api_client.post(
        f"{base_url}/api/walks/{walk['id']}/tips/checkout",
        json={"quick_amount": 5, "origin_url": base_url},
        timeout=30,
    )
    assert response.status_code == 200

    data = response.json()
    tip_cleanup_scope["sessions"].append(data["session_id"])
    assert data["session_id"]
    assert data["tip_id"]
    assert data["checkout_url"].startswith("http")
    assert float(data["amount"]) == 5.0


def test_tip_checkout_status_returns_tip_state(api_client, base_url, tip_cleanup_scope):
    walk = _create_and_finish_walk(api_client, base_url, f"status_{uuid.uuid4().hex[:6]}")
    tip_cleanup_scope["walk_ids"].append(walk["id"])

    checkout = api_client.post(
        f"{base_url}/api/walks/{walk['id']}/tips/checkout",
        json={"quick_amount": 5, "origin_url": base_url},
        timeout=30,
    )
    if checkout.status_code != 200:
        pytest.skip(f"Checkout Stripe indisponível neste ambiente: {checkout.status_code} {checkout.text}")

    session_id = checkout.json()["session_id"]
    tip_cleanup_scope["sessions"].append(session_id)

    status_response = api_client.get(f"{base_url}/api/tips/checkout/status/{session_id}", timeout=30)
    assert status_response.status_code == 200
    payload = status_response.json()
    assert payload["session_id"] == session_id
    assert payload["walk_id"] == walk["id"]
    assert payload["payment_status"]


def test_single_paid_tip_per_walk_is_enforced(api_client, base_url, tip_cleanup_scope):
    walk = _create_and_finish_walk(api_client, base_url, f"single_{uuid.uuid4().hex[:6]}")
    tip_cleanup_scope["walk_ids"].append(walk["id"])

    tip_id = f"TEST_ITER26_TIP_{uuid.uuid4().hex[:8]}"
    now_iso = datetime.now(timezone.utc).isoformat()
    mongo_client, db = _mongo_db()
    try:
        db.tips.insert_one(
            {
                "id": tip_id,
                "walk_id": walk["id"],
                "client_user_id": "test-client",
                "client_name": "TEST_ITER26 Client",
                "walker_user_id": str(walk.get("walker_user_id") or ""),
                "walker_id": str(walk.get("walker_id") or "walker-1"),
                "walker_name": str(walk.get("walker_name") or "Walker"),
                "amount": 10.0,
                "currency": "brl",
                "status": "paid",
                "payment_status": "paid",
                "checkout_session_id": f"cs_iter26_{uuid.uuid4().hex[:12]}",
                "checkout_url": "https://example.com/checkout",
                "tip_deadline_at": now_iso,
                "paid_at": now_iso,
                "suspicious_flag": False,
                "created_at": now_iso,
                "updated_at": now_iso,
            }
        )
    finally:
        mongo_client.close()
    tip_cleanup_scope["tip_ids"].append(tip_id)

    response = api_client.post(
        f"{base_url}/api/walks/{walk['id']}/tips/checkout",
        json={"quick_amount": 5},
        timeout=25,
    )
    assert response.status_code == 400


def test_walker_tip_summary_returns_today_month_historical(base_url, tip_cleanup_scope):
    walker_session = _login_session(base_url, "passeador@petpasso.com", "Passeador@123")
    admin_session = _login_session(base_url, "superadmin@petpasso.com", "SuperAdmin@123")
    try:
        me = walker_session.get(f"{base_url}/api/auth/me", timeout=25)
        assert me.status_code == 200
        walker_user_id = me.json().get("id")
        assert walker_user_id

        walk = _create_and_finish_walk(admin_session, base_url, f"summary_{uuid.uuid4().hex[:6]}")
        tip_cleanup_scope["walk_ids"].append(walk["id"])

        now = datetime.now(timezone.utc)
        last_month = now - timedelta(days=35)
        tip_id_today = f"TEST_ITER26_TODAY_{uuid.uuid4().hex[:8]}"
        tip_id_old = f"TEST_ITER26_OLD_{uuid.uuid4().hex[:8]}"

        mongo_client, db = _mongo_db()
        try:
            for tip_id, amount, paid_at in [
                (tip_id_today, 12.0, now.isoformat()),
                (tip_id_old, 8.0, last_month.isoformat()),
            ]:
                db.tips.insert_one(
                    {
                        "id": tip_id,
                        "walk_id": walk["id"],
                        "client_user_id": "client-test",
                        "client_name": "TEST_ITER26 Cliente",
                        "walker_user_id": walker_user_id,
                        "walker_id": str(walk.get("walker_id") or "walker-1"),
                        "walker_name": "Passeador",
                        "amount": amount,
                        "currency": "brl",
                        "status": "paid",
                        "payment_status": "paid",
                        "checkout_session_id": f"cs_{uuid.uuid4().hex[:12]}",
                        "checkout_url": "https://example.com/checkout",
                        "tip_deadline_at": now.isoformat(),
                        "paid_at": paid_at,
                        "suspicious_flag": False,
                        "created_at": now.isoformat(),
                        "updated_at": now.isoformat(),
                    }
                )
        finally:
            mongo_client.close()

        tip_cleanup_scope["tip_ids"].extend([tip_id_today, tip_id_old])

        summary = walker_session.get(f"{base_url}/api/walker/tips/summary", timeout=25)
        assert summary.status_code == 200
        payload = summary.json()
        assert float(payload["today_total"]) >= 12.0
        assert float(payload["month_total"]) >= 12.0
        assert float(payload["historical_total"]) >= 20.0
    finally:
        walker_session.close()
        admin_session.close()


def test_admin_tips_audit_lists_paid_tips(base_url, tip_cleanup_scope):
    admin_session = _login_session(base_url, "superadmin@petpasso.com", "SuperAdmin@123")
    try:
        walk = _create_and_finish_walk(admin_session, base_url, f"audit_{uuid.uuid4().hex[:6]}")
        tip_cleanup_scope["walk_ids"].append(walk["id"])

        tip_id = f"TEST_ITER26_AUDIT_{uuid.uuid4().hex[:8]}"
        now_iso = datetime.now(timezone.utc).isoformat()

        mongo_client, db = _mongo_db()
        try:
            db.tips.insert_one(
                {
                    "id": tip_id,
                    "walk_id": walk["id"],
                    "client_user_id": "audit-client",
                    "client_name": "TEST_ITER26 Audit",
                    "walker_user_id": str(walk.get("walker_user_id") or ""),
                    "walker_id": str(walk.get("walker_id") or "walker-1"),
                    "walker_name": str(walk.get("walker_name") or "Passeador"),
                    "amount": 15.0,
                    "currency": "brl",
                    "status": "paid",
                    "payment_status": "paid",
                    "checkout_session_id": f"cs_audit_{uuid.uuid4().hex[:8]}",
                    "checkout_url": "https://example.com/checkout",
                    "tip_deadline_at": now_iso,
                    "paid_at": now_iso,
                    "suspicious_flag": False,
                    "created_at": now_iso,
                    "updated_at": now_iso,
                }
            )
        finally:
            mongo_client.close()
        tip_cleanup_scope["tip_ids"].append(tip_id)

        response = admin_session.get(f"{base_url}/api/admin/tips", timeout=25)
        assert response.status_code == 200
        rows = response.json()
        assert any(row.get("id") == tip_id for row in rows)
    finally:
        admin_session.close()


def test_auth_playbook_login_sets_http_only_cookies(base_url):
    response = requests.post(
        f"{base_url}/api/auth/login",
        json={"email": "cliente@petpasso.com", "password": "Cliente@123"},
        timeout=25,
    )
    assert response.status_code == 200
    set_cookie = response.headers.get("set-cookie", "").lower()
    assert "access_token=" in set_cookie
    assert "refresh_token=" in set_cookie
    assert "httponly" in set_cookie


def test_auth_playbook_cors_preflight_credentials_explicit_origin(base_url):
    origin = base_url.rstrip("/")
    response = requests.options(
        f"{base_url}/api/auth/login",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
        timeout=25,
    )
    assert response.status_code in (200, 204)
    assert response.headers.get("access-control-allow-credentials") == "true"
    assert response.headers.get("access-control-allow-origin") == origin


def test_auth_playbook_bcrypt_prefix_and_lockout(base_url):
    mongo_client, db = _mongo_db()
    try:
        seeded_admin = db.users.find_one({"email": "admin@petpasso.com"}, {"password_hash": 1})
        assert seeded_admin is not None
        assert str(seeded_admin.get("password_hash", "")).startswith("$2b$")
    finally:
        mongo_client.close()

    attacker_email = f"iter26_lockout_{uuid.uuid4().hex[:8]}@petpasso.com"
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


def test_auth_playbook_seed_admin_updates_existing_password_hash(base_url):
    mongo_client, db = _mongo_db()
    admin_email = "admin@petpasso.com"
    admin_password = "Admin@123"

    try:
        mutated = bcrypt.hashpw("Changed@999".encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
        db.login_attempts.delete_many({"identifier": {"$regex": f":{admin_email}$"}})
        db.users.update_one({"email": admin_email}, {"$set": {"password_hash": mutated}})

        before_seed = requests.post(
            f"{base_url}/api/auth/login",
            json={"email": admin_email, "password": admin_password},
            timeout=25,
        )
        assert before_seed.status_code == 401

        if "/app/backend" not in sys.path:
            sys.path.append("/app/backend")
        import server as backend_server  # type: ignore

        asyncio.run(backend_server.seed_auth_and_indexes())
        db.login_attempts.delete_many({"identifier": {"$regex": f":{admin_email}$"}})

        after_seed = requests.post(
            f"{base_url}/api/auth/login",
            json={"email": admin_email, "password": admin_password},
            timeout=25,
        )
        assert after_seed.status_code == 200
    finally:
        mongo_client.close()
