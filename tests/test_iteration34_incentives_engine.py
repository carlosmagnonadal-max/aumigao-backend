import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests
from dotenv import dotenv_values
from pymongo import MongoClient


# Iteration 34 - Incentives engine and auth hardening checks


def _mongo_db():
    backend_env = Path("/app/backend/.env")
    values = dotenv_values(backend_env) if backend_env.exists() else {}
    mongo_url = (os.environ.get("MONGO_URL") or values.get("MONGO_URL") or "").strip().strip('"')
    db_name = (os.environ.get("DB_NAME") or values.get("DB_NAME") or "").strip().strip('"')
    if not mongo_url or not db_name:
        pytest.skip("MONGO_URL/DB_NAME não configurados")
    client = MongoClient(mongo_url)
    return client, client[db_name]


def _login(base_url: str, email: str, password: str) -> requests.Session:
    session = requests.Session()
    response = session.post(
        f"{base_url}/api/auth/login",
        json={"email": email, "password": password},
        timeout=20,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    token = body.get("access_token")
    assert token and isinstance(token, str)
    session.headers.update({"Authorization": f"Bearer {token}"})
    return session


def _first_walker_for_client(client_session: requests.Session, base_url: str) -> dict:
    response = client_session.get(
        f"{base_url}/api/walkers?date=2026-04-20&duration_minutes=30&preferred_time=09:00",
        timeout=20,
    )
    assert response.status_code == 200, response.text
    walkers = response.json()
    assert isinstance(walkers, list) and walkers, "Nenhum passeador retornado"
    return walkers[0]


class TestIncentivesAdminSettings:
    """Admin incentive settings and defaults"""

    def test_get_incentive_settings_defaults(self, base_url: str):
        admin = _login(base_url, "superadmin@petpasso.com", "SuperAdmin@123")
        response = admin.get(f"{base_url}/api/admin/incentives/settings", timeout=20)
        assert response.status_code == 200, response.text
        data = response.json()

        assert data["walker_share_percent"] == pytest.approx(80.0)
        assert data["platform_share_percent"] == pytest.approx(20.0)
        assert data["consistency_bonus_amount"] == pytest.approx(30.0)
        assert data["consistency_days_required"] == 7
        assert data["critical_hour_bonus_amount"] == pytest.approx(5.0)
        assert data["critical_windows"] == [{"start": "06:00", "end": "09:59"}, {"start": "16:00", "end": "21:59"}]
        assert data["volume_bonus_tiers"] == [
            {"target_walks": 20, "amount": 30.0},
            {"target_walks": 40, "amount": 70.0},
            {"target_walks": 60, "amount": 120.0},
        ]

    def test_patch_incentive_settings_persists(self, base_url: str):
        admin = _login(base_url, "superadmin@petpasso.com", "SuperAdmin@123")

        patch_payload = {
            "walker_share_percent": 78,
            "platform_share_percent": 22,
            "critical_hour_bonus_amount": 6,
            "consistency_bonus_amount": 35,
        }
        patch_response = admin.patch(
            f"{base_url}/api/admin/incentives/settings",
            json=patch_payload,
            timeout=20,
        )
        assert patch_response.status_code == 200, patch_response.text
        patched = patch_response.json()
        assert patched["walker_share_percent"] == pytest.approx(78.0)
        assert patched["platform_share_percent"] == pytest.approx(22.0)
        assert patched["critical_hour_bonus_amount"] == pytest.approx(6.0)
        assert patched["consistency_bonus_amount"] == pytest.approx(35.0)

        get_again = admin.get(f"{base_url}/api/admin/incentives/settings", timeout=20)
        assert get_again.status_code == 200
        persisted = get_again.json()
        assert persisted["walker_share_percent"] == pytest.approx(78.0)
        assert persisted["platform_share_percent"] == pytest.approx(22.0)


class TestIncentivesWalkerAndWalkSplit:
    """Walker summary and walk split fields"""

    def test_walker_incentives_summary_structure(self, base_url: str):
        walker = _login(base_url, "passeador@petpasso.com", "Passeador@123")
        response = walker.get(f"{base_url}/api/walker/incentives/summary", timeout=20)
        assert response.status_code == 200, response.text
        payload = response.json()

        assert isinstance(payload.get("week_earnings"), (int, float))
        assert isinstance(payload.get("month_earnings"), (int, float))
        assert isinstance(payload.get("historical_earnings"), (int, float))
        assert isinstance(payload.get("active_bonuses"), list)
        assert isinstance(payload.get("progress_items"), list)
        assert isinstance(payload.get("recent_bonus_history"), list)

    def test_walk_creation_records_split_fields(self, base_url: str):
        client = _login(base_url, "cliente@petpasso.com", "Cliente@123")
        walker = _first_walker_for_client(client, base_url)
        walker_id = str(walker.get("id") or "")
        assert walker_id

        test_suffix = datetime.now(timezone.utc).strftime("%H%M%S")
        payload = {
            "pet_name": f"TEST_INC_PET_{test_suffix}",
            "client_name": "TEST_INC_CLIENT",
            "walk_date": "2026-04-20",
            "walk_time": "09:00",
            "duration_minutes": 30,
            "walk_type": "Individual",
            "walker_id": walker_id,
            "pickup_street": "Rua Teste",
            "pickup_number": "10",
            "pickup_neighborhood": "Pituba",
            "pickup_complement": "Ap 1",
            "location_reference": "Portão azul",
            "pet_behavior_notes": "TEST",
            "notes": "TEST_ITER34_INC_SPLIT",
        }
        create_response = client.post(f"{base_url}/api/walks", json=payload, timeout=25)
        assert create_response.status_code == 201, create_response.text
        created = create_response.json()

        assert isinstance(created.get("charged_amount"), (int, float))
        assert isinstance(created.get("walker_payout_amount"), (int, float))
        assert isinstance(created.get("platform_retained_amount"), (int, float))
        assert isinstance(created.get("walker_share_percent"), (int, float))
        assert isinstance(created.get("platform_fee_percent"), (int, float))
        assert created["walker_share_percent"] == pytest.approx(78.0)
        assert created["platform_fee_percent"] == pytest.approx(22.0)
        assert round(created["walker_share_percent"] + created["platform_fee_percent"], 2) == pytest.approx(100.0)


class TestIncentivesNoShowInvalidationAndBonusHistory:
    """Bonus invalidation on walker no-show and history collection"""

    def test_week_bonus_invalidated_after_walker_no_show(self, base_url: str):
        admin = _login(base_url, "superadmin@petpasso.com", "SuperAdmin@123")
        client = _login(base_url, "cliente@petpasso.com", "Cliente@123")
        walker_session = _login(base_url, "passeador@petpasso.com", "Passeador@123")

        me = walker_session.get(f"{base_url}/api/auth/me", timeout=20)
        assert me.status_code == 200
        walker_user_id = me.json()["id"]

        walker = _first_walker_for_client(client, base_url)
        payload = {
            "pet_name": f"TEST_NO_SHOW_{datetime.now(timezone.utc).strftime('%H%M%S')}",
            "client_name": "TEST_NO_SHOW_CLIENT",
            "walk_date": "2026-04-21",
            "walk_time": "16:30",
            "duration_minutes": 30,
            "walk_type": "Individual",
            "walker_id": walker["id"],
            "pickup_street": "Rua Teste",
            "pickup_number": "21",
            "pickup_neighborhood": "Pituba",
            "pickup_complement": "Casa",
            "location_reference": "TEST",
            "pet_behavior_notes": "TEST",
            "notes": "TEST_ITER34_NO_SHOW",
        }
        create_resp = client.post(f"{base_url}/api/walks", json=payload, timeout=25)
        assert create_resp.status_code == 201, create_resp.text
        walk_id = create_resp.json()["id"]

        mongo_client, database = _mongo_db()
        try:
            walk_doc = database.walks.find_one({"id": walk_id}, {"_id": 0, "walk_datetime_iso": 1})
            assert walk_doc and walk_doc.get("walk_datetime_iso")
            walk_dt = datetime.fromisoformat(str(walk_doc["walk_datetime_iso"]))
            week_start = walk_dt - timedelta(days=walk_dt.weekday())
            week_key = week_start.date().isoformat()

            fake_bonus = {
                "id": f"TEST_BONUS_{walk_id}",
                "bonus_key": f"test-bonus-{walk_id}",
                "walker_user_id": walker_user_id,
                "bonus_type": "consistency_week",
                "amount": 30.0,
                "status": "active",
                "description": "TEST bonus",
                "walk_id": walk_id,
                "week_key": week_key,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            database.walker_bonus_payments.update_one(
                {"bonus_key": fake_bonus["bonus_key"]},
                {"$set": fake_bonus},
                upsert=True,
            )

            status_response = admin.patch(
                f"{base_url}/api/admin/walks/{walk_id}/status",
                json={"status": "Não comparecimento do passeador"},
                timeout=25,
            )
            assert status_response.status_code == 200, status_response.text

            invalidated = database.walker_bonus_payments.find_one(
                {"bonus_key": fake_bonus["bonus_key"]},
                {"_id": 0, "status": 1, "invalidated_reason": 1},
            )
            assert invalidated["status"] == "invalidated"
            assert "No-show" in str(invalidated.get("invalidated_reason") or "")
        finally:
            mongo_client.close()


class TestIncentivesRankingAndAuthPlaybook:
    """Ranking bonus integration and auth playbook checks"""

    def test_walker_ranking_contains_bonus_related_fields(self, base_url: str):
        client = _login(base_url, "cliente@petpasso.com", "Cliente@123")
        response = client.get(
            f"{base_url}/api/walkers?date=2026-04-20&duration_minutes=30&preferred_time=09:00&neighborhood=pituba",
            timeout=20,
        )
        assert response.status_code == 200, response.text
        rows = response.json()
        assert isinstance(rows, list)
        assert rows, "Lista de passeadores vazia"

        top = rows[0]
        assert "quality_status" in top
        assert "selection_reason" in top
        assert isinstance(top.get("selection_reason"), str)

    def test_auth_login_sets_http_only_cookies(self, base_url: str):
        response = requests.post(
            f"{base_url}/api/auth/login",
            json={"email": "admin@petpasso.com", "password": "Admin@123"},
            timeout=20,
        )
        assert response.status_code == 200, response.text
        cookie_headers = ",".join(response.raw.headers.get_all("Set-Cookie") or [])
        assert "access_token=" in cookie_headers
        assert "refresh_token=" in cookie_headers
        assert "HttpOnly" in cookie_headers

    def test_bcrypt_hash_format_in_seeded_admin(self):
        mongo_client, database = _mongo_db()
        try:
            admin = database.users.find_one({"email": "admin@petpasso.com"}, {"_id": 0, "password_hash": 1})
            assert admin and isinstance(admin.get("password_hash"), str)
            assert admin["password_hash"].startswith("$2b$")
        finally:
            mongo_client.close()

    def test_bruteforce_lockout_after_five_failures(self, base_url: str):
        email = "admin@petpasso.com"
        session = requests.Session()
        statuses = []
        for _ in range(6):
            r = session.post(
                f"{base_url}/api/auth/login",
                json={"email": email, "password": "WrongPassword!123"},
                timeout=20,
            )
            statuses.append(r.status_code)

        assert statuses.count(401) >= 5
        assert 429 in statuses

        # cleanup lockout to avoid affecting subsequent tests
        mongo_client, database = _mongo_db()
        try:
            database.login_attempts.delete_many({"identifier": {"$regex": f":{email}$"}})
        finally:
            mongo_client.close()

    def test_cors_preflight_allows_credentials_with_explicit_origin(self, base_url: str):
        origin = base_url
        response = requests.options(
            f"{base_url}/api/auth/login",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
            timeout=20,
        )
        allow_origin = response.headers.get("Access-Control-Allow-Origin", "")
        allow_credentials = response.headers.get("Access-Control-Allow-Credentials", "")

        assert response.status_code in {200, 204}
        assert allow_origin == origin
        assert allow_credentials.lower() == "true"
