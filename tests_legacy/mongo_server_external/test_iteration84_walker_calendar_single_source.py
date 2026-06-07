from __future__ import annotations

import uuid
from datetime import date, timedelta
import os

import pytest
import requests
from pymongo import MongoClient
from dotenv import dotenv_values


# Module: auth playbook checks (bcrypt/cookies/CORS/lockout)
# Module: walker availability contracts for daily capacity overrides

WALKER_CREDS = {"email": "walker@petpasso.com", "password": "Walker@123"}
ADMIN_CREDS = {"email": "admin@petpasso.com", "password": "Admin@123"}


def _mongo_db():
    backend_env = dotenv_values("/app/backend/.env")
    mongo_url = os.environ.get("MONGO_URL") or backend_env.get("MONGO_URL")
    db_name = os.environ.get("DB_NAME") or backend_env.get("DB_NAME")
    if not mongo_url or not db_name:
        pytest.skip("MONGO_URL/DB_NAME não configurados")
    client = MongoClient(str(mongo_url).strip().strip('"'))
    return client, client[str(db_name).strip().strip('"')]


def _target_iso(days_ahead: int = 2) -> str:
    return (date.today() + timedelta(days=days_ahead)).strftime("%Y-%m-%d")


def _login(base_url: str, email: str, password: str) -> requests.Session:
    session = requests.Session()
    response = session.post(
        f"{base_url}/api/auth/login",
        json={"email": email, "password": password},
        timeout=30,
    )
    assert response.status_code == 200, f"Login falhou ({email}): {response.status_code} {response.text}"
    payload = response.json() if response.text else {}
    token = payload.get("access_token")
    assert token, "Token de acesso ausente no login"
    session.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/json"})
    return session


@pytest.fixture()
def walker_availability_context(base_url: str):
    walker = _login(base_url, WALKER_CREDS["email"], WALKER_CREDS["password"])
    original_resp = walker.get(f"{base_url}/api/walker/availability-settings", timeout=30)
    assert original_resp.status_code == 200, original_resp.text
    original = original_resp.json()
    try:
        yield walker, original
    finally:
        restore_payload = {
            "availability_days": original.get("availability_days", ["seg", "ter", "qua", "qui", "sex"]),
            "availability_start_time": original.get("availability_start_time", "08:00"),
            "availability_end_time": original.get("availability_end_time", "18:00"),
            "availability_periods": original.get("availability_periods", {}),
            "availability_capacity_by_period": original.get(
                "availability_capacity_by_period", {"manha": 3, "tarde": 3, "noite": 2}
            ),
            "availability_daily_capacity_overrides": original.get("availability_daily_capacity_overrides", {}),
        }
        walker.patch(f"{base_url}/api/walker/availability", json=restore_payload, timeout=30)
        walker.close()


def test_auth_bcrypt_hash_format_starts_with_2b():
    mongo_client, db = _mongo_db()
    try:
        admin = db.users.find_one({"email": ADMIN_CREDS["email"]}, {"_id": 0, "password_hash": 1})
        assert admin is not None
        assert str(admin.get("password_hash") or "").startswith("$2b$")
    finally:
        mongo_client.close()


def test_auth_login_sets_http_only_cookies_and_me_works(base_url: str):
    session = requests.Session()
    login = session.post(
        f"{base_url}/api/auth/login",
        json={"email": ADMIN_CREDS["email"], "password": ADMIN_CREDS["password"]},
        timeout=30,
    )
    assert login.status_code == 200, login.text
    set_cookie = "\n".join(
        login.raw.headers.get_all("Set-Cookie") if login.raw and login.raw.headers else [login.headers.get("set-cookie", "")]
    )
    assert "access_token=" in set_cookie and "refresh_token=" in set_cookie
    assert "HttpOnly" in set_cookie

    me = session.get(f"{base_url}/api/auth/me", timeout=30)
    assert me.status_code == 200, me.text
    assert me.json().get("email") == ADMIN_CREDS["email"]
    session.close()


def test_auth_cors_preflight_allows_credentials(base_url: str):
    origin = base_url.rstrip("/")
    preflight = requests.options(
        f"{base_url}/api/auth/login",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
        timeout=30,
    )
    assert preflight.status_code in {200, 204}
    assert preflight.headers.get("access-control-allow-credentials") == "true"
    assert preflight.headers.get("access-control-allow-origin") == origin


def test_auth_brute_force_lockout_after_5_failed_attempts(base_url: str):
    unique_ip = f"198.51.100.{int(uuid.uuid4().hex[:2], 16)}"
    statuses = []
    for _ in range(6):
        resp = requests.post(
            f"{base_url}/api/auth/login",
            json={"email": ADMIN_CREDS["email"], "password": "SenhaErrada@123"},
            headers={"x-forwarded-for": unique_ip},
            timeout=30,
        )
        statuses.append(resp.status_code)

    assert statuses[:5] == [401, 401, 401, 401, 401]
    assert statuses[5] == 429


def test_walker_availability_accepts_daily_overrides_and_settings_returns_them(base_url: str, walker_availability_context):
    walker, original = walker_availability_context
    target_day = _target_iso(3)

    payload = {
        "availability_days": original.get("availability_days", ["seg", "ter", "qua", "qui", "sex"]),
        "availability_start_time": original.get("availability_start_time", "08:00"),
        "availability_end_time": original.get("availability_end_time", "18:00"),
        "availability_periods": original.get("availability_periods", {}),
        "availability_capacity_by_period": original.get(
            "availability_capacity_by_period", {"manha": 3, "tarde": 3, "noite": 2}
        ),
        "availability_daily_capacity_overrides": {
            target_day: 7,
            "invalid-date": 99,
            _target_iso(4): 0,
        },
    }

    update = walker.patch(f"{base_url}/api/walker/availability", json=payload, timeout=30)
    assert update.status_code == 200, update.text
    update_body = update.json()

    # Contract expected by feature: override field should also be returned in update response.
    assert "availability_daily_capacity_overrides" in update_body

    settings = walker.get(f"{base_url}/api/walker/availability-settings", timeout=30)
    assert settings.status_code == 200, settings.text
    settings_body = settings.json()
    overrides = settings_body.get("availability_daily_capacity_overrides")
    assert isinstance(overrides, dict)
    assert overrides.get(target_day) == 7
    assert "invalid-date" not in overrides


def test_walker_availability_settings_returns_overrides_after_update(base_url: str, walker_availability_context):
    walker, original = walker_availability_context
    target_day = _target_iso(5)

    payload = {
        "availability_days": original.get("availability_days", ["seg", "ter", "qua", "qui", "sex"]),
        "availability_start_time": original.get("availability_start_time", "08:00"),
        "availability_end_time": original.get("availability_end_time", "18:00"),
        "availability_periods": original.get("availability_periods", {}),
        "availability_capacity_by_period": original.get(
            "availability_capacity_by_period", {"manha": 3, "tarde": 3, "noite": 2}
        ),
        "availability_daily_capacity_overrides": {target_day: 5},
    }
    update = walker.patch(f"{base_url}/api/walker/availability", json=payload, timeout=30)
    assert update.status_code == 200, update.text

    settings = walker.get(f"{base_url}/api/walker/availability-settings", timeout=30)
    assert settings.status_code == 200, settings.text
    body = settings.json()
    overrides = body.get("availability_daily_capacity_overrides")
    assert isinstance(overrides, dict)
    assert overrides.get(target_day) == 5
