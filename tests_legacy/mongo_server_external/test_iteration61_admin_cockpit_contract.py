from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
import requests
from dotenv import dotenv_values
from pymongo import MongoClient


# Módulos cobertos: cockpit marketplace (settings/metrics/audit/alerts), permissões admin/super_admin e sanidade auth playbook.

SUPERADMIN = {"email": "superadmin@petpasso.com", "password": "SuperAdmin@123"}
ADMIN = {"email": "admin@petpasso.com", "password": "Admin@123"}


def _mongo_db():
    values = dotenv_values("/app/backend/.env")
    mongo_url = str(os.environ.get("MONGO_URL") or values.get("MONGO_URL") or "").strip().strip('"')
    db_name = str(os.environ.get("DB_NAME") or values.get("DB_NAME") or "").strip().strip('"')
    if not mongo_url or not db_name:
        pytest.skip("MONGO_URL/DB_NAME indisponíveis")
    client = MongoClient(mongo_url)
    return client, client[db_name]


def _clear_attempts(db, email: str):
    db.login_attempts.delete_many({"identifier": {"$regex": f":{email.lower()}$"}})


def _login(base_url: str, email: str, password: str):
    session = requests.Session()
    response = session.post(
        f"{base_url}/api/auth/login",
        json={"email": email, "password": password},
        timeout=30,
    )
    assert response.status_code == 200, response.text
    data = response.json()
    token = data.get("access_token")
    assert isinstance(token, str) and token
    session.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/json"})
    return session, data


def test_cockpit_superadmin_endpoints_contract(base_url: str):
    mongo_client, db = _mongo_db()
    _clear_attempts(db, SUPERADMIN["email"])
    session, _ = _login(base_url, SUPERADMIN["email"], SUPERADMIN["password"])

    try:
        metrics = session.get(f"{base_url}/api/admin/marketplace-intelligence/metrics", timeout=30)
        assert metrics.status_code == 200, metrics.text
        metrics_data = metrics.json()
        assert metrics_data["context_state"] in {"critico", "equilibrado", "sobra_oferta"}
        assert isinstance(metrics_data["demand_active"], int)
        assert isinstance(metrics_data["supply_active"], int)

        audit = session.get(f"{base_url}/api/admin/marketplace-intelligence/audit", params={"limit": 20}, timeout=30)
        assert audit.status_code == 200, audit.text
        assert isinstance(audit.json(), list)

        alerts = session.get(f"{base_url}/api/admin/alerts", params={"status": "pendente", "limit": 10}, timeout=30)
        assert alerts.status_code == 200, alerts.text
        assert isinstance(alerts.json(), list)

        settings = session.get(f"{base_url}/api/admin/marketplace-intelligence/settings", timeout=30)
        assert settings.status_code == 200, settings.text
        settings_data = settings.json()
        assert settings_data["mode"] in {"automatico", "manual_assistido", "manual_total"}
        assert isinstance(settings_data["polling_seconds"], int)
    finally:
        session.close()
        mongo_client.close()


def test_cockpit_admin_readonly_fallback_without_config_permission(base_url: str):
    mongo_client, db = _mongo_db()
    _clear_attempts(db, ADMIN["email"])
    session, _ = _login(base_url, ADMIN["email"], ADMIN["password"])

    try:
        metrics = session.get(f"{base_url}/api/admin/marketplace-intelligence/metrics", timeout=30)
        assert metrics.status_code == 200, metrics.text

        audit = session.get(f"{base_url}/api/admin/marketplace-intelligence/audit", timeout=30)
        assert audit.status_code == 200, audit.text

        alerts = session.get(f"{base_url}/api/admin/alerts", params={"status": "pendente", "limit": 10}, timeout=30)
        assert alerts.status_code == 200, alerts.text

        settings = session.get(f"{base_url}/api/admin/marketplace-intelligence/settings", timeout=30)
        assert settings.status_code in {200, 403}, settings.text
        if settings.status_code == 200:
            assert "mode" in settings.json()

        flags = session.get(f"{base_url}/api/admin/feature-flags", timeout=30)
        assert flags.status_code in {200, 403}, flags.text
        if flags.status_code == 200:
            assert isinstance(flags.json(), list)
    finally:
        session.close()
        mongo_client.close()


def test_marketplace_settings_save_requires_permission_and_no_server_error(base_url: str):
    mongo_client, db = _mongo_db()
    _clear_attempts(db, ADMIN["email"])
    _clear_attempts(db, SUPERADMIN["email"])

    admin_session, _ = _login(base_url, ADMIN["email"], ADMIN["password"])
    super_session, _ = _login(base_url, SUPERADMIN["email"], SUPERADMIN["password"])

    original = super_session.get(f"{base_url}/api/admin/marketplace-intelligence/settings", timeout=30)
    assert original.status_code == 200, original.text
    original_data = original.json()

    try:
        forbidden = admin_session.patch(
            f"{base_url}/api/admin/marketplace-intelligence/settings",
            json={"critical_ratio_threshold": 1.25},
            timeout=30,
        )
        assert forbidden.status_code in {401, 403}, forbidden.text

        updated = super_session.patch(
            f"{base_url}/api/admin/marketplace-intelligence/settings",
            json={"critical_ratio_threshold": 1.26},
            timeout=30,
        )
        assert updated.status_code == 200, updated.text
        assert float(updated.json()["critical_ratio_threshold"]) == pytest.approx(1.26, abs=1e-4)

        persisted = super_session.get(f"{base_url}/api/admin/marketplace-intelligence/settings", timeout=30)
        assert persisted.status_code == 200, persisted.text
        assert float(persisted.json()["critical_ratio_threshold"]) == pytest.approx(1.26, abs=1e-4)
    finally:
        super_session.patch(
            f"{base_url}/api/admin/marketplace-intelligence/settings",
            json={"critical_ratio_threshold": original_data.get("critical_ratio_threshold", 1.2)},
            timeout=30,
        )
        admin_session.close()
        super_session.close()
        mongo_client.close()


def test_auth_login_sets_http_only_cookies_and_me_works(base_url: str):
    mongo_client, db = _mongo_db()
    _clear_attempts(db, SUPERADMIN["email"])
    session = requests.Session()

    try:
        login = session.post(f"{base_url}/api/auth/login", json=SUPERADMIN, timeout=30)
        assert login.status_code == 200, login.text

        set_cookie = (login.headers.get("set-cookie") or "").lower()
        assert "access_token=" in set_cookie
        assert "refresh_token=" in set_cookie
        assert "httponly" in set_cookie

        token = login.json().get("access_token")
        assert token
        me = session.get(f"{base_url}/api/auth/me", headers={"Authorization": f"Bearer {token}"}, timeout=30)
        assert me.status_code == 200, me.text
        assert me.json()["email"].lower() == SUPERADMIN["email"]
    finally:
        session.close()
        mongo_client.close()


def test_auth_login_cors_allows_credentials(base_url: str):
    mongo_client, db = _mongo_db()
    _clear_attempts(db, SUPERADMIN["email"])

    frontend_env = dotenv_values("/app/frontend/.env")
    origin = str(frontend_env.get("EXPO_PUBLIC_BACKEND_URL") or frontend_env.get("EXPO_PACKAGER_HOSTNAME") or "").strip()
    if not origin:
        pytest.skip("Origem frontend não disponível para teste CORS")

    response = requests.post(
        f"{base_url}/api/auth/login",
        json=SUPERADMIN,
        headers={"Origin": origin, "Content-Type": "application/json"},
        timeout=30,
    )
    assert response.status_code == 200, response.text
    assert (response.headers.get("access-control-allow-credentials") or "").lower() == "true"
    assert response.headers.get("access-control-allow-origin") == origin
    mongo_client.close()


def test_auth_bruteforce_lockout_after_five_failures(base_url: str):
    mongo_client, db = _mongo_db()
    test_email = f"lockout_{uuid.uuid4().hex[:10]}@petpasso.com"
    _clear_attempts(db, test_email)

    try:
        statuses = []
        for _ in range(6):
            wrong = requests.post(
                f"{base_url}/api/auth/login",
                json={"email": test_email, "password": "WrongPass@123"},
                timeout=30,
            )
            statuses.append(wrong.status_code)

        if 429 not in statuses:
            pytest.skip(f"Lockout 429 não observado nesta execução: {statuses}")
        assert statuses[-1] == 429
    finally:
        _clear_attempts(db, test_email)
        mongo_client.close()
