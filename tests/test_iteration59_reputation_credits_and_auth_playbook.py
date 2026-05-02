import os
import uuid
from datetime import date, timedelta
from pathlib import Path

import bcrypt
import pytest
import requests
from dotenv import dotenv_values
from pymongo import MongoClient


# Module coverage: reputation credits (CR), ranking consistency, walker tasks regression, and auth playbook checks.


def _login_session(base_url: str, email: str, password: str) -> requests.Session:
    session = requests.Session()
    response = session.post(
        f"{base_url}/api/auth/login",
        json={"email": email, "password": password},
        timeout=35,
    )
    if response.status_code != 200:
        session.close()
        pytest.skip(f"Login indisponível para {email}: {response.status_code}")
    payload = response.json() or {}
    token = payload.get("access_token")
    if not token:
        session.close()
        pytest.skip(f"Token ausente para {email}")
    session.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/json"})
    return session


@pytest.fixture(scope="session")
def mongo_db():
    backend_env = Path("/app/backend/.env")
    values = dotenv_values(backend_env) if backend_env.exists() else {}
    mongo_url = os.environ.get("MONGO_URL") or values.get("MONGO_URL")
    db_name = os.environ.get("DB_NAME") or values.get("DB_NAME")

    if not mongo_url or not db_name:
        pytest.skip("MONGO_URL/DB_NAME não configurados")

    client = MongoClient(str(mongo_url).strip().strip('"'))
    database = client[str(db_name).strip().strip('"')]
    yield database
    client.close()


@pytest.fixture()
def walker_scope(base_url: str, mongo_db):
    session = _login_session(base_url, "walker@petpasso.com", "Walker@123")
    me_resp = session.get(f"{base_url}/api/auth/me", timeout=35)
    assert me_resp.status_code == 200, me_resp.text
    me_data = me_resp.json() or {}
    walker_user_id = str(me_data.get("id") or "")
    walker_name = str(me_data.get("full_name") or "")
    if not walker_user_id:
        session.close()
        pytest.skip("walker id ausente em /api/auth/me")

    snapshot = (
        mongo_db.users.find_one(
            {"id": walker_user_id},
            {
                "_id": 0,
                "reputation_credits": 1,
                "last_credit_update": 1,
                "cr_daily_uses_date": 1,
                "cr_daily_uses_count": 1,
                "cr_matching_boost_until": 1,
                "cr_early_wave_until": 1,
                "cr_visual_highlight_until": 1,
                "cr_matching_boost_points_active": 1,
                "cr_early_wave_priority_active": 1,
                "cr_visual_exposure_points_active": 1,
                "verification_level": 1,
                "is_verified": 1,
            },
        )
        or {}
    )

    yield {
        "session": session,
        "walker_user_id": walker_user_id,
        "walker_name": walker_name,
        "snapshot": snapshot,
    }

    restore = {
        "reputation_credits": snapshot.get("reputation_credits", 0),
        "last_credit_update": snapshot.get("last_credit_update"),
        "cr_daily_uses_date": snapshot.get("cr_daily_uses_date"),
        "cr_daily_uses_count": snapshot.get("cr_daily_uses_count", 0),
        "cr_matching_boost_until": snapshot.get("cr_matching_boost_until"),
        "cr_early_wave_until": snapshot.get("cr_early_wave_until"),
        "cr_visual_highlight_until": snapshot.get("cr_visual_highlight_until"),
        "cr_matching_boost_points_active": snapshot.get("cr_matching_boost_points_active", 5.0),
        "cr_early_wave_priority_active": snapshot.get("cr_early_wave_priority_active", 1.0),
        "cr_visual_exposure_points_active": snapshot.get("cr_visual_exposure_points_active", 1.0),
        "verification_level": snapshot.get("verification_level", "NONE"),
        "is_verified": snapshot.get("is_verified", False),
    }
    mongo_db.users.update_one({"id": walker_user_id}, {"$set": restore})
    session.close()


def _find_walker_row(client: requests.Session, base_url: str, walker_name: str) -> tuple[str, dict]:
    for offset in range(0, 7):
        target_date = (date.today() + timedelta(days=offset)).isoformat()
        query_options = [
            {"date": target_date, "duration_minutes": 30, "tipo_passeio": "padrao"},
            {"date": target_date, "duration_minutes": 30, "neighborhood": "Centro", "tipo_passeio": "padrao"},
        ]
        for params in query_options:
            response = client.get(
                f"{base_url}/api/walkers",
                params=params,
                timeout=35,
            )
            if response.status_code != 200:
                continue
            rows = response.json() if isinstance(response.json(), list) else []
            for row in rows:
                if str(row.get("name") or "").strip().lower() == walker_name.strip().lower():
                    return target_date, row
    pytest.skip("Não foi possível localizar o passeador no ranking /api/walkers")


def test_get_reputation_credits_contract_and_multipliers(base_url: str, walker_scope, mongo_db):
    # CR contract: saldo, multiplicadores, limites e flags ativas.
    walker_user_id = walker_scope["walker_user_id"]
    session = walker_scope["session"]

    mongo_db.users.update_one(
        {"id": walker_user_id},
        {
            "$set": {
                "verification_level": "PREMIUM",
                "is_verified": True,
                "reputation_credits": 21,
                "cr_daily_uses_date": date.today().isoformat(),
                "cr_daily_uses_count": 1,
                "cr_matching_boost_until": None,
                "cr_early_wave_until": None,
                "cr_visual_highlight_until": None,
            }
        },
    )

    response = session.get(f"{base_url}/api/walker/reputation-credits", timeout=35)
    assert response.status_code == 200, response.text
    data = response.json()

    assert data.get("reputation_credits") == 21
    assert data.get("daily_uses_limit") == 3
    assert data.get("daily_uses_count") == 1
    assert data.get("daily_uses_remaining") == 2
    assert data.get("verification_level") == "PREMIUM"
    assert float(data.get("gain_multiplier") or 0) == pytest.approx(1.2, abs=1e-6)
    assert float(data.get("premium_cost_multiplier") or 0) == pytest.approx(0.8, abs=1e-6)
    assert float(data.get("premium_effect_multiplier") or 0) == pytest.approx(1.2, abs=1e-6)


def test_use_reputation_credits_costs_and_daily_limit(base_url: str, walker_scope, mongo_db):
    # CR use flow: custos corretos por ação e bloqueio no 4º uso diário.
    walker_user_id = walker_scope["walker_user_id"]
    session = walker_scope["session"]

    mongo_db.users.update_one(
        {"id": walker_user_id},
        {
            "$set": {
                "verification_level": "PREMIUM",
                "is_verified": True,
                "reputation_credits": 30,
                "cr_daily_uses_date": date.today().isoformat(),
                "cr_daily_uses_count": 0,
                "cr_matching_boost_until": None,
                "cr_early_wave_until": None,
                "cr_visual_highlight_until": None,
                "cr_matching_boost_points_active": 5.0,
                "cr_early_wave_priority_active": 1.0,
                "cr_visual_exposure_points_active": 1.0,
            }
        },
    )

    first = session.post(f"{base_url}/api/walker/reputation-credits/use", json={"action": "matching_boost"}, timeout=35)
    assert first.status_code == 200, first.text
    first_data = first.json()
    assert first_data.get("reputation_credits") == 26
    assert first_data.get("daily_uses_count") == 1
    assert first_data.get("is_matching_boost_active") is True
    assert float(first_data.get("premium_effect_multiplier") or 0) == pytest.approx(1.2, abs=1e-6)

    second = session.post(f"{base_url}/api/walker/reputation-credits/use", json={"action": "early_wave"}, timeout=35)
    assert second.status_code == 200, second.text
    second_data = second.json()
    assert second_data.get("reputation_credits") == 23
    assert second_data.get("daily_uses_count") == 2
    assert second_data.get("is_early_wave_active") is True

    third = session.post(f"{base_url}/api/walker/reputation-credits/use", json={"action": "visual_highlight"}, timeout=35)
    assert third.status_code == 200, third.text
    third_data = third.json()
    assert third_data.get("reputation_credits") == 21
    assert third_data.get("daily_uses_count") == 3
    assert third_data.get("is_visual_highlight_active") is True

    fourth = session.post(f"{base_url}/api/walker/reputation-credits/use", json={"action": "matching_boost"}, timeout=35)
    assert fourth.status_code == 400
    assert "Limite diário" in fourth.text


def test_matching_boost_reflects_active_flag_and_non_regression_in_walkers(base_url: str, walker_scope, mongo_db):
    # Ranking consistency: após matching_boost, score/ranking não regrede e flag ativa reflete.
    walker_user_id = walker_scope["walker_user_id"]
    walker_name = walker_scope["walker_name"]
    walker_session = walker_scope["session"]
    client_session = _login_session(base_url, "cliente@petpasso.com", "Cliente@123")

    try:
        mongo_db.users.update_one(
            {"id": walker_user_id},
            {
                "$set": {
                    "verification_level": "PREMIUM",
                    "is_verified": True,
                    "reputation_credits": 20,
                    "cr_daily_uses_date": date.today().isoformat(),
                    "cr_daily_uses_count": 0,
                    "cr_matching_boost_until": None,
                    "cr_visual_highlight_until": None,
                    "cr_early_wave_until": None,
                    "cr_matching_boost_points_active": 5.0,
                }
            },
        )

        target_date, before_row = _find_walker_row(client_session, base_url, walker_name)
        before_score = float(before_row.get("ranking_score_final") or before_row.get("match_score") or 0.0)

        use_resp = walker_session.post(
            f"{base_url}/api/walker/reputation-credits/use",
            json={"action": "matching_boost"},
            timeout=35,
        )
        assert use_resp.status_code == 200, use_resp.text

        after_resp = client_session.get(
            f"{base_url}/api/walkers",
            params={"date": target_date, "duration_minutes": 30, "neighborhood": "Centro", "tipo_passeio": "padrao"},
            timeout=35,
        )
        assert after_resp.status_code == 200, after_resp.text
        after_rows = after_resp.json() if isinstance(after_resp.json(), list) else []

        after_row = next(
            (row for row in after_rows if str(row.get("name") or "").strip().lower() == walker_name.strip().lower()),
            None,
        )
        if not after_row:
            pytest.skip("Passeador não encontrado após ativação do boost")

        after_score = float(after_row.get("ranking_score_final") or after_row.get("match_score") or 0.0)
        assert bool(after_row.get("cr_matching_boost_active")) is True
        assert after_score >= before_score
    finally:
        client_session.close()


def test_walker_tasks_endpoint_regression_no_500_and_required_field(base_url: str, walker_scope):
    # Regression check: /walker/tasks deve permanecer 200 e conter walk_datetime_iso nos itens.
    session = walker_scope["session"]
    response = session.get(f"{base_url}/api/walker/tasks", timeout=35)
    assert response.status_code == 200, response.text
    rows = response.json() if isinstance(response.json(), list) else []
    assert isinstance(rows, list)
    if rows:
        assert all("walk_datetime_iso" in row for row in rows)


def test_auth_login_sets_http_only_cookies(base_url: str):
    # Auth playbook: login deve definir cookies httpOnly.
    response = requests.post(
        f"{base_url}/api/auth/login",
        json={"email": "admin@petpasso.com", "password": "Admin@123"},
        timeout=35,
    )
    assert response.status_code == 200, response.text
    set_cookie_raw = ", ".join(response.raw.headers.get_all("Set-Cookie") or [])
    assert "access_token=" in set_cookie_raw
    assert "refresh_token=" in set_cookie_raw
    assert "HttpOnly" in set_cookie_raw


def test_auth_cors_preflight_credentials_with_explicit_origin(base_url: str):
    # Auth playbook: CORS deve permitir credenciais e origin explícita (não '*').
    origin = "https://petpasso-mvp.preview.emergentagent.com"
    response = requests.options(
        f"{base_url}/api/auth/login",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
        timeout=35,
    )
    assert response.status_code in {200, 204}
    assert response.headers.get("access-control-allow-origin") == origin
    assert response.headers.get("access-control-allow-credentials") == "true"


def test_auth_bruteforce_lockout_after_five_failures(base_url: str):
    # Auth playbook: lockout após 5 falhas.
    unique_ip = f"198.51.100.{(uuid.uuid4().int % 200) + 10}"
    headers = {"X-Forwarded-For": unique_ip}

    for _ in range(5):
        fail = requests.post(
            f"{base_url}/api/auth/login",
            json={"email": "admin@petpasso.com", "password": "SenhaInvalida!"},
            headers=headers,
            timeout=35,
        )
        assert fail.status_code == 401

    locked = requests.post(
        f"{base_url}/api/auth/login",
        json={"email": "admin@petpasso.com", "password": "SenhaInvalida!"},
        headers=headers,
        timeout=35,
    )
    assert locked.status_code == 429


def test_admin_password_hash_is_bcrypt_2b_and_matches_seed_credential(mongo_db):
    # Auth playbook: hash bcrypt $2b$ e credencial seed válida para admin.
    row = mongo_db.users.find_one({"email": "admin@petpasso.com"}, {"_id": 0, "password_hash": 1})
    assert row is not None
    password_hash = str(row.get("password_hash") or "")
    assert password_hash.startswith("$2b$")
    assert bcrypt.checkpw("Admin@123".encode("utf-8"), password_hash.encode("utf-8"))