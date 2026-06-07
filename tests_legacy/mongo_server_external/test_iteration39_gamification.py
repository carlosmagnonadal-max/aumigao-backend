import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests
from dotenv import dotenv_values
from pymongo import MongoClient

sys.path.append("/app/backend")

from server import (
    WALKER_LEVEL_BRONZE,
    WALKER_LEVEL_ELITE,
    WALKER_LEVEL_OURO,
    WALKER_LEVEL_PRATA,
    WALKER_LEVEL_PRIORITY_BONUS,
    WEEKLY_TIP_GOAL_AMOUNT,
    _determine_walker_level,
    _gamification_badges,
    _hash_password,
    _verify_password,
    _weekly_mission_progress,
    seed_auth_and_indexes,
)


# Module coverage: walker gamification levels, matching bonuses, missions, weekly tip goal, badges, and auth playbook checks


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
        timeout=25,
    )
    assert response.status_code == 200, response.text
    token = response.json().get("access_token")
    assert token
    session.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/json"})
    return session


class TestGamificationBusinessRules:
    def test_determine_level_elite_requires_no_show_under_or_equal_3_percent(self):
        elite = _determine_walker_level(score_final=95.0, completed_walks=60, no_show_rate=3.0)
        downgraded = _determine_walker_level(score_final=95.0, completed_walks=60, no_show_rate=3.1)
        assert elite == WALKER_LEVEL_ELITE
        assert downgraded == WALKER_LEVEL_OURO

    def test_determine_level_score_and_volume_thresholds(self):
        assert _determine_walker_level(score_final=74.9, completed_walks=20, no_show_rate=0.0) == WALKER_LEVEL_BRONZE
        assert _determine_walker_level(score_final=75.0, completed_walks=10, no_show_rate=0.0) == WALKER_LEVEL_PRATA
        assert _determine_walker_level(score_final=85.0, completed_walks=25, no_show_rate=2.0) == WALKER_LEVEL_OURO

    def test_matching_priority_bonus_mapping_by_level(self):
        assert WALKER_LEVEL_PRIORITY_BONUS[WALKER_LEVEL_BRONZE] == pytest.approx(0.0)
        assert WALKER_LEVEL_PRIORITY_BONUS[WALKER_LEVEL_PRATA] == pytest.approx(0.01)
        assert WALKER_LEVEL_PRIORITY_BONUS[WALKER_LEVEL_OURO] == pytest.approx(0.015)
        assert WALKER_LEVEL_PRIORITY_BONUS[WALKER_LEVEL_ELITE] == pytest.approx(0.02)

    def test_weekly_missions_5_walks_2_five_stars_1_tip_enable_bonus(self):
        finished_five_star = [
            {"status": "Finalizado", "rating": 5},
            {"status": "Finalizado", "rating": 5},
            {"status": "Finalizado", "rating": 4},
            {"status": "Finalizado", "rating": 5},
            {"status": "Finalizado", "rating": 4},
        ]
        paid_tips = [{"status": "paid"}]
        mission = _weekly_mission_progress(finished_five_star, paid_tips)

        assert mission["completed_all"] is True
        assert mission["mission_bonus_points"] == pytest.approx(2.0)
        keys = {item["key"] for item in mission["missions"]}
        assert keys == {"mission_walks_5", "mission_rating_5star_2", "mission_tip_1"}

    def test_badges_generation_supports_requested_labels(self):
        metrics = {
            "tip_recent_window_total": 45.0,
            "rating_weighted_avg": 4.9,
            "rating_count": 15,
            "punctuality_rate": 0.97,
            "severe_delay_rate": 1.0,
            "recency_factor": 1.03,
            "score_last_7": 94.0,
            "score_reference": 88.0,
        }
        badges = _gamification_badges(metrics, week_tip_total=21.0)
        assert "Pet favorito" in badges
        assert "5 estrelas" in badges
        assert "Pontual" in badges
        assert "Em alta" in badges


class TestGamificationApiContracts:
    def test_walker_incentive_summary_returns_gamification_fields(self, base_url: str):
        walker = _login(base_url, "passeador@petpasso.com", "Passeador@123")
        response = walker.get(f"{base_url}/api/walker/incentives/summary", timeout=25)
        assert response.status_code == 200, response.text
        body = response.json()

        assert body["walker_level"] in {"bronze", "prata", "ouro", "elite"}
        assert isinstance(body.get("missions"), list)
        assert isinstance(body.get("gamification_badges"), list)
        assert body.get("weekly_tip_goal") == pytest.approx(WEEKLY_TIP_GOAL_AMOUNT)
        assert isinstance(body.get("weekly_tip_total"), (int, float))
        assert isinstance(body.get("weekly_tip_goal_reached"), bool)
        assert body.get("mission_bonus_value") == pytest.approx(0.02)

    def test_walkers_ranking_contains_level_and_badges_fields(self, base_url: str):
        client = _login(base_url, "cliente@petpasso.com", "Cliente@123")
        response = client.get(
            f"{base_url}/api/walkers?date=2026-04-22&duration_minutes=30&preferred_time=09:00&neighborhood=pituba",
            timeout=25,
        )
        assert response.status_code == 200, response.text
        rows = response.json()
        assert isinstance(rows, list)
        assert rows

        top = rows[0]
        assert top.get("walker_level") in {"bronze", "prata", "ouro", "elite"}
        assert isinstance(top.get("gamification_badges", []), list)
        assert isinstance(top.get("weekly_tip_goal_reached"), bool)
        assert isinstance(top.get("mission_bonus_points"), (int, float))

        for row in rows:
            level = row.get("walker_level")
            if level in WALKER_LEVEL_PRIORITY_BONUS:
                assert float(row.get("level_priority_bonus", 0.0)) == pytest.approx(WALKER_LEVEL_PRIORITY_BONUS[level])


class TestAuthPlaybookRegression:
    def test_bcrypt_hash_starts_with_2b_for_seed_admin(self):
        mongo_client, database = _mongo_db()
        try:
            admin = database.users.find_one({"email": "admin@petpasso.com"}, {"_id": 0, "password_hash": 1})
            assert admin and isinstance(admin.get("password_hash"), str)
            assert admin["password_hash"].startswith("$2b$")
        finally:
            mongo_client.close()

    def test_auth_login_sets_http_only_cookies(self, base_url: str):
        response = requests.post(
            f"{base_url}/api/auth/login",
            json={"email": "cliente@petpasso.com", "password": "Cliente@123"},
            timeout=25,
        )
        assert response.status_code == 200
        set_cookie = ",".join(response.raw.headers.get_all("Set-Cookie") or [])
        assert "access_token=" in set_cookie
        assert "refresh_token=" in set_cookie
        assert "HttpOnly" in set_cookie

    def test_auth_cors_allows_credentials_with_explicit_origin(self, base_url: str):
        response = requests.options(
            f"{base_url}/api/auth/login",
            headers={
                "Origin": base_url,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
            timeout=25,
        )
        assert response.status_code in {200, 204}
        assert response.headers.get("Access-Control-Allow-Origin") == base_url
        assert response.headers.get("Access-Control-Allow-Credentials", "").lower() == "true"

    def test_bruteforce_lockout_after_five_fails(self, base_url: str):
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

        mongo_client, database = _mongo_db()
        try:
            database.login_attempts.delete_many({"identifier": {"$regex": f":{email}$"}})
        finally:
            mongo_client.close()

    def test_seed_admin_updates_existing_password_when_outdated(self):
        mongo_client, database = _mongo_db()
        admin_email = "admin@petpasso.com"
        expected_password = "Admin@123"
        row = database.users.find_one({"email": admin_email}, {"_id": 0, "id": 1, "password_hash": 1})
        if not row:
            mongo_client.close()
            pytest.skip("Admin seed não encontrado")

        admin_id = row["id"]
        original_hash = str(row.get("password_hash") or "")
        try:
            database.users.update_one(
                {"id": admin_id},
                {"$set": {"password_hash": _hash_password("TempWrong@123")}},
            )

            asyncio.run(seed_auth_and_indexes())

            refreshed = database.users.find_one({"id": admin_id}, {"_id": 0, "password_hash": 1})
            assert refreshed and _verify_password(expected_password, str(refreshed.get("password_hash") or ""))
        finally:
            if original_hash:
                database.users.update_one({"id": admin_id}, {"$set": {"password_hash": original_hash}})
            mongo_client.close()