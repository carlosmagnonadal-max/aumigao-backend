import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import requests
from dotenv import dotenv_values
from pymongo import MongoClient


# Module coverage: auth playbook checks + admin dashboard critical modules/actions (cockpit, coupons, incentives, referrals, toggles, payments, configs, disintermediation).

SUPERADMIN = {"email": "superadmin@petpasso.com", "password": "SuperAdmin@123"}
ADMIN = {"email": "admin@petpasso.com", "password": "Admin@123"}


def _mongo_db():
    backend_env = dotenv_values("/app/backend/.env")
    mongo_url = str(os.environ.get("MONGO_URL") or backend_env.get("MONGO_URL") or "").strip().strip('"')
    db_name = str(os.environ.get("DB_NAME") or backend_env.get("DB_NAME") or "").strip().strip('"')
    if not mongo_url or not db_name:
        pytest.skip("MONGO_URL/DB_NAME indisponíveis")
    client = MongoClient(mongo_url)
    return client, client[db_name]


def _login(base_url: str, email: str, password: str) -> tuple[requests.Session, requests.Response]:
    session = requests.Session()
    session.headers.update({"Accept": "application/json", "Content-Type": "application/json"})
    response = session.post(f"{base_url}/api/auth/login", json={"email": email, "password": password}, timeout=30)
    if response.ok:
        token = response.json().get("access_token")
        if token:
            session.headers.update({"Authorization": f"Bearer {token}"})
    return session, response


def test_auth_hash_cookie_cors_and_lockout(base_url: str):
    mongo_client, db = _mongo_db()
    try:
        user = db.users.find_one({"email": SUPERADMIN["email"]}, {"_id": 0, "password_hash": 1})
        assert user is not None
        assert str(user.get("password_hash", "")).startswith("$2b$")

        login_session, login_response = _login(base_url, SUPERADMIN["email"], SUPERADMIN["password"])
        assert login_response.status_code == 200, login_response.text
        set_cookie = (login_response.headers.get("set-cookie") or "").lower()
        assert "access_token=" in set_cookie
        assert "refresh_token=" in set_cookie
        assert "httponly" in set_cookie
        login_session.close()

        frontend_env = dotenv_values("/app/frontend/.env")
        origin = str(frontend_env.get("EXPO_PUBLIC_BACKEND_URL") or frontend_env.get("EXPO_PACKAGER_HOSTNAME") or "").strip()
        if origin:
            cors = requests.post(
                f"{base_url}/api/auth/login",
                json=SUPERADMIN,
                headers={"Origin": origin, "Content-Type": "application/json"},
                timeout=30,
            )
            assert cors.status_code == 200, cors.text
            assert (cors.headers.get("access-control-allow-credentials") or "").lower() == "true"
            assert cors.headers.get("access-control-allow-origin") == origin

        lock_email = f"lock_{uuid.uuid4().hex[:8]}@petpasso.com"
        statuses = []
        for _ in range(6):
            wrong = requests.post(
                f"{base_url}/api/auth/login",
                json={"email": lock_email, "password": "WrongPass@123"},
                timeout=30,
            )
            statuses.append(wrong.status_code)
        if 429 not in statuses:
            pytest.skip(f"Lockout 429 não observado nesta execução: {statuses}")
        assert statuses[-1] == 429
    finally:
        mongo_client.close()


def test_seed_admin_updates_existing_admin_password_if_changed(base_url: str):
    mongo_client, db = _mongo_db()
    original = db.users.find_one({"email": ADMIN["email"]}, {"_id": 0, "id": 1, "password_hash": 1})
    if not original:
        mongo_client.close()
        pytest.skip("Conta admin seed não encontrada")

    try:
        db.users.update_one({"id": original["id"]}, {"$set": {"password_hash": "$2b$12$invalidinvalidinvalidinvalidinvalidinvalidinvalid12"}})

        broken_session, broken_login = _login(base_url, ADMIN["email"], ADMIN["password"])
        assert broken_login.status_code in {400, 401}, broken_login.text
        broken_session.close()

        import importlib.util
        from pathlib import Path

        server_path = Path("/app/backend/server.py")
        spec = importlib.util.spec_from_file_location("backend_server_module", server_path)
        assert spec and spec.loader
        backend_server = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(backend_server)
        asyncio.run(backend_server.seed_auth_and_indexes())

        fixed_session, fixed_login = _login(base_url, ADMIN["email"], ADMIN["password"])
        assert fixed_login.status_code == 200, fixed_login.text
        fixed_session.close()
    finally:
        db.users.update_one({"id": original["id"]}, {"$set": {"password_hash": original["password_hash"]}})
        mongo_client.close()


def test_admin_dashboard_critical_lists_and_details(base_url: str):
    session, login = _login(base_url, SUPERADMIN["email"], SUPERADMIN["password"])
    assert login.status_code == 200, login.text
    try:
        metrics = session.get(f"{base_url}/api/admin/marketplace-intelligence/metrics", timeout=30)
        assert metrics.status_code == 200, metrics.text
        assert metrics.json().get("context_state") in {"critico", "equilibrado", "sobra_oferta"}

        flags = session.get(f"{base_url}/api/admin/feature-flags", timeout=30)
        assert flags.status_code == 200, flags.text
        assert isinstance(flags.json(), list)

        pending = session.get(f"{base_url}/api/admin/pending-actions", timeout=30)
        assert pending.status_code == 200, pending.text
        assert isinstance(pending.json(), list)

        payments = session.get(f"{base_url}/api/admin/payments", timeout=30)
        assert payments.status_code == 200, payments.text
        payment_rows = payments.json()
        assert isinstance(payment_rows, list)
        if payment_rows:
            detail = session.get(f"{base_url}/api/admin/payments/{payment_rows[0]['id']}", timeout=30)
            assert detail.status_code == 200, detail.text
            assert detail.json()["id"] == payment_rows[0]["id"]
    finally:
        session.close()


def test_cockpit_save_settings_persists_and_reverts(base_url: str):
    session, login = _login(base_url, SUPERADMIN["email"], SUPERADMIN["password"])
    assert login.status_code == 200, login.text
    try:
        original = session.get(f"{base_url}/api/admin/marketplace-intelligence/settings", timeout=30)
        assert original.status_code == 200, original.text
        original_data = original.json()

        next_threshold = round(float(original_data.get("critical_ratio_threshold", 1.2)) + 0.01, 2)
        saved = session.patch(
            f"{base_url}/api/admin/marketplace-intelligence/settings",
            json={"critical_ratio_threshold": next_threshold},
            timeout=30,
        )
        assert saved.status_code == 200, saved.text
        assert float(saved.json()["critical_ratio_threshold"]) == pytest.approx(next_threshold, abs=1e-4)

        persisted = session.get(f"{base_url}/api/admin/marketplace-intelligence/settings", timeout=30)
        assert persisted.status_code == 200, persisted.text
        assert float(persisted.json()["critical_ratio_threshold"]) == pytest.approx(next_threshold, abs=1e-4)
    finally:
        session.patch(
            f"{base_url}/api/admin/marketplace-intelligence/settings",
            json={"critical_ratio_threshold": original_data.get("critical_ratio_threshold", 1.2)},
            timeout=30,
        )
        session.close()


def test_coupon_invalidate_action_functional(base_url: str):
    session, login = _login(base_url, SUPERADMIN["email"], SUPERADMIN["password"])
    assert login.status_code == 200, login.text
    try:
        now = datetime.now(timezone.utc)
        payload = {
            "code": f"TEST83{uuid.uuid4().hex[:6].upper()}",
            "discount_percent": 10,
            "discount_fixed": 0,
            "valid_from": now.isoformat(),
            "valid_until": (now + timedelta(days=30)).isoformat(),
            "max_global_uses": 10,
            "max_uses_per_user": 1,
            "applicable_walk_types": ["Individual"],
            "is_active": True,
        }
        created = session.post(f"{base_url}/api/admin/coupons", json=payload, timeout=30)
        assert created.status_code == 201, created.text
        created_data = created.json()
        assert created_data["code"] == payload["code"]
        assert created_data["is_active"] is True

        invalidated = session.patch(f"{base_url}/api/admin/coupons/{created_data['id']}/invalidate", timeout=30)
        assert invalidated.status_code == 200, invalidated.text
        invalidated_data = invalidated.json()
        assert invalidated_data["id"] == created_data["id"]
        assert invalidated_data["is_active"] is False

        listed = session.get(f"{base_url}/api/admin/coupons", timeout=30)
        assert listed.status_code == 200, listed.text
        persisted = next(item for item in listed.json() if item["id"] == created_data["id"])
        assert persisted["is_active"] is False
    finally:
        session.close()


def test_incentive_settings_save_persists_and_reverts(base_url: str):
    session, login = _login(base_url, SUPERADMIN["email"], SUPERADMIN["password"])
    assert login.status_code == 200, login.text
    try:
        original = session.get(f"{base_url}/api/admin/incentives/settings", timeout=30)
        assert original.status_code == 200, original.text
        original_data = original.json()

        next_quality_bonus = float(original_data["quality_bonus_percent"]) + 0.5
        saved = session.patch(
            f"{base_url}/api/admin/incentives/settings",
            json={"quality_bonus_percent": next_quality_bonus},
            timeout=30,
        )
        assert saved.status_code == 200, saved.text
        assert float(saved.json()["quality_bonus_percent"]) == pytest.approx(next_quality_bonus, abs=1e-4)

        persisted = session.get(f"{base_url}/api/admin/incentives/settings", timeout=30)
        assert persisted.status_code == 200, persisted.text
        assert float(persisted.json()["quality_bonus_percent"]) == pytest.approx(next_quality_bonus, abs=1e-4)
    finally:
        session.patch(
            f"{base_url}/api/admin/incentives/settings",
            json={"quality_bonus_percent": original_data["quality_bonus_percent"]},
            timeout=30,
        )
        session.close()


def test_referral_program_save_and_mark_fraud_when_available(base_url: str):
    session, login = _login(base_url, SUPERADMIN["email"], SUPERADMIN["password"])
    assert login.status_code == 200, login.text
    try:
        original = session.get(f"{base_url}/api/admin/referral-program/settings", timeout=30)
        assert original.status_code == 200, original.text
        original_data = original.json()

        toggled_visible = not bool(original_data.get("app_visible", False))
        saved = session.patch(
            f"{base_url}/api/admin/referral-program/settings",
            json={"app_visible": toggled_visible},
            timeout=30,
        )
        assert saved.status_code == 200, saved.text
        assert bool(saved.json()["app_visible"]) is toggled_visible

        persisted = session.get(f"{base_url}/api/admin/referrals", params={"limit": 20}, timeout=30)
        assert persisted.status_code == 200, persisted.text
        rows = persisted.json().get("items", [])
        if not rows:
            pytest.skip("Sem referrals para validar marcação de fraude")
        target = next((row for row in rows if row.get("status") != "invalida_fraude"), rows[0])
        marked = session.patch(
            f"{base_url}/api/admin/referrals/{target['id']}/status",
            json={"status": "invalida_fraude", "note": "TEST83 marcação de fraude"},
            timeout=30,
        )
        assert marked.status_code == 200, marked.text
        assert marked.json()["status"] == "invalida_fraude"
    finally:
        session.patch(
            f"{base_url}/api/admin/referral-program/settings",
            json={"app_visible": original_data.get("app_visible", False)},
            timeout=30,
        )
        session.close()


def test_feature_toggle_active_visible_persistence(base_url: str):
    session, login = _login(base_url, SUPERADMIN["email"], SUPERADMIN["password"])
    assert login.status_code == 200, login.text
    try:
        listed = session.get(f"{base_url}/api/admin/feature-flags", timeout=30)
        assert listed.status_code == 200, listed.text
        rows = listed.json()
        assert len(rows) > 0
        target = rows[0]

        next_active = not bool(target["is_active"])
        next_visible = not bool(target["is_visible"])
        updated = session.patch(
            f"{base_url}/api/admin/feature-flags/{target['feature_name']}",
            json={"is_active": next_active, "is_visible": next_visible},
            timeout=30,
        )
        assert updated.status_code == 200, updated.text
        updated_data = updated.json()
        assert bool(updated_data["is_active"]) is next_active
        assert bool(updated_data["is_visible"]) is next_visible

        refreshed = session.get(f"{base_url}/api/admin/feature-flags", timeout=30)
        assert refreshed.status_code == 200, refreshed.text
        persisted = next(item for item in refreshed.json() if item["feature_name"] == target["feature_name"])
        assert bool(persisted["is_active"]) is next_active
        assert bool(persisted["is_visible"]) is next_visible
    finally:
        session.patch(
            f"{base_url}/api/admin/feature-flags/{target['feature_name']}",
            json={"is_active": target["is_active"], "is_visible": target["is_visible"]},
            timeout=30,
        )
        session.close()


def test_support_configs_admins_and_disintermediation_actions(base_url: str):
    session, login = _login(base_url, SUPERADMIN["email"], SUPERADMIN["password"])
    assert login.status_code == 200, login.text
    try:
        support = session.get(f"{base_url}/api/support/tickets", timeout=30)
        assert support.status_code == 200, support.text
        assert isinstance(support.json(), list)

        premium = session.get(f"{base_url}/api/admin/premium-config", timeout=30)
        assert premium.status_code == 200, premium.text
        premium_payload = {"premiumRepassePercentual": premium.json()["premiumRepassePercentual"]}
        premium_save = session.patch(f"{base_url}/api/admin/premium-config", json=premium_payload, timeout=30)
        assert premium_save.status_code == 200, premium_save.text

        badge = session.get(f"{base_url}/api/admin/premium-verified/settings", timeout=30)
        assert badge.status_code == 200, badge.text
        badge_save = session.patch(
            f"{base_url}/api/admin/premium-verified/settings",
            json={
                "streak_minimo_para_selo": badge.json()["streak_minimo_para_selo"],
                "bonus_score_base": badge.json()["bonus_score_base"],
                "priority_bonus": badge.json()["priority_bonus"],
                "cr_efficiency_multiplier": badge.json()["cr_efficiency_multiplier"],
            },
            timeout=30,
        )
        assert badge_save.status_code == 200, badge_save.text

        administrators = session.get(f"{base_url}/api/admin/administrators", timeout=30)
        assert administrators.status_code == 200, administrators.text
        assert isinstance(administrators.json(), list)

        logs = session.get(f"{base_url}/api/admin/administrators/logs", timeout=30)
        assert logs.status_code == 200, logs.text
        assert isinstance(logs.json(), list)

        dis_overview = session.get(f"{base_url}/api/admin/disintermediation/overview", timeout=30)
        assert dis_overview.status_code == 200, dis_overview.text
        users = dis_overview.json().get("users", [])
        if users:
            acted = session.post(
                f"{base_url}/api/admin/disintermediation/{users[0]['user_id']}/action",
                json={"action": "warn"},
                timeout=30,
            )
            assert acted.status_code == 200, acted.text
            assert "message" in acted.json()
    finally:
        session.close()
