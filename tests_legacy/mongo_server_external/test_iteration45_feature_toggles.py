import os
import uuid
from pathlib import Path

import pytest
import requests
from dotenv import dotenv_values
from pymongo import MongoClient


# Module coverage: feature-flag admin/public APIs, guard behavior for tips/incentives/badges/highlights, and key auth guardrails.


def _mongo_db():
    backend_env = dotenv_values("/app/backend/.env")
    mongo_url = os.environ.get("MONGO_URL") or backend_env.get("MONGO_URL")
    db_name = os.environ.get("DB_NAME") or backend_env.get("DB_NAME")
    if not mongo_url or not db_name:
        pytest.skip("MONGO_URL/DB_NAME não configurados")
    client = MongoClient(str(mongo_url).strip().strip('"'))
    db = client[str(db_name).strip().strip('"')]
    return client, db


def _login(base_url: str, email: str, password: str) -> requests.Session:
    session = requests.Session()
    response = session.post(
        f"{base_url}/api/auth/login",
        json={"email": email, "password": password},
        timeout=35,
    )
    assert response.status_code == 200, response.text
    token = response.json().get("access_token")
    assert token
    session.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/json"})
    return session


@pytest.fixture()
def feature_flags_scope(base_url: str):
    admin_session = _login(base_url, "superadmin@petpasso.com", "SuperAdmin@123")
    baseline_response = admin_session.get(f"{base_url}/api/admin/feature-flags", timeout=35)
    assert baseline_response.status_code == 200, baseline_response.text
    baseline = {row["feature_name"]: row for row in baseline_response.json()}

    scope = {
        "admin": admin_session,
        "baseline": baseline,
        "touched": set(),
        "lock_identifier": None,
    }

    yield scope

    for feature_name in scope["touched"]:
        original = baseline.get(feature_name)
        if not original:
            continue
        admin_session.patch(
            f"{base_url}/api/admin/feature-flags/{feature_name}",
            json={"is_active": bool(original.get("is_active", False)), "is_visible": bool(original.get("is_visible", False))},
            timeout=35,
        )

    if scope["lock_identifier"]:
        mongo_client, db = _mongo_db()
        try:
            db.login_attempts.delete_many({"identifier": scope["lock_identifier"]})
        finally:
            mongo_client.close()

    admin_session.close()


def _set_flag(scope, base_url: str, feature_name: str, *, is_active: bool, is_visible: bool):
    scope["touched"].add(feature_name)
    response = scope["admin"].patch(
        f"{base_url}/api/admin/feature-flags/{feature_name}",
        json={"is_active": is_active, "is_visible": is_visible},
        timeout=35,
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["feature_name"] == feature_name
    assert bool(payload["is_active"]) is is_active
    assert bool(payload["is_visible"]) is is_visible
    return payload


def test_auth_seed_admin_hash_uses_bcrypt_2b_prefix():
    mongo_client, db = _mongo_db()
    try:
        admin_row = db.users.find_one({"email": "superadmin@petpasso.com"}, {"_id": 0, "password_hash": 1})
    finally:
        mongo_client.close()

    assert admin_row is not None
    assert str(admin_row.get("password_hash") or "").startswith("$2b$")


def test_auth_login_sets_http_only_cookies(base_url: str):
    response = requests.post(
        f"{base_url}/api/auth/login",
        json={"email": "superadmin@petpasso.com", "password": "SuperAdmin@123"},
        timeout=35,
    )
    assert response.status_code == 200, response.text
    set_cookie = response.headers.get("set-cookie", "")
    assert "access_token=" in set_cookie
    assert "refresh_token=" in set_cookie
    assert "HttpOnly" in set_cookie


def test_auth_cors_preflight_allows_credentials_with_explicit_origin(base_url: str):
    response = requests.options(
        f"{base_url}/api/auth/login",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
        timeout=35,
    )
    assert response.status_code in (200, 204)
    assert response.headers.get("access-control-allow-credentials") == "true"
    assert response.headers.get("access-control-allow-origin") in {
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    }


def test_auth_lockout_after_five_failed_attempts(base_url: str, feature_flags_scope):
    test_ip = "203.0.113.77"
    identifier = f"{test_ip}:superadmin@petpasso.com"
    feature_flags_scope["lock_identifier"] = identifier

    mongo_client, db = _mongo_db()
    try:
        db.login_attempts.delete_many({"identifier": identifier})
    finally:
        mongo_client.close()

    for _ in range(5):
        fail_response = requests.post(
            f"{base_url}/api/auth/login",
            json={"email": "superadmin@petpasso.com", "password": "wrong-pass"},
            headers={"x-forwarded-for": test_ip},
            timeout=35,
        )
        assert fail_response.status_code == 401

    lock_response = requests.post(
        f"{base_url}/api/auth/login",
        json={"email": "superadmin@petpasso.com", "password": "wrong-pass"},
        headers={"x-forwarded-for": test_ip},
        timeout=35,
    )
    assert lock_response.status_code == 429


def test_get_admin_feature_flags_lists_catalog_with_groups(base_url: str, feature_flags_scope):
    response = feature_flags_scope["admin"].get(f"{base_url}/api/admin/feature-flags", timeout=35)
    assert response.status_code == 200, response.text
    rows = response.json()
    assert len(rows) >= 9

    by_name = {row["feature_name"]: row for row in rows}
    assert "tips" in by_name
    assert "habit_incentive" in by_name
    assert by_name["tips"]["group"] == "monetizacao_incentivos"
    assert by_name["visible_badges"]["group"] == "visibilidade_ranking"


def test_patch_feature_flag_updates_state_and_writes_audit(base_url: str, feature_flags_scope):
    updated = _set_flag(feature_flags_scope, base_url, "usage_streak", is_active=True, is_visible=True)

    mongo_client, db = _mongo_db()
    try:
        audit_row = db.feature_flag_audit.find_one(
            {
                "feature_name": "usage_streak",
                "updated_by": str(updated.get("updated_by") or ""),
                "is_active": True,
                "is_visible": True,
            },
            {"_id": 0},
            sort=[("updated_at", -1)],
        )
    finally:
        mongo_client.close()

    assert audit_row is not None
    assert audit_row.get("feature_name") == "usage_streak"


def test_patch_feature_flag_blocks_inconsistent_state(base_url: str, feature_flags_scope):
    response = feature_flags_scope["admin"].patch(
        f"{base_url}/api/admin/feature-flags/tips",
        json={"is_active": False, "is_visible": True},
        timeout=35,
    )
    assert response.status_code == 400
    assert "Estado inconsistente" in response.text


def test_patch_unknown_feature_returns_404(base_url: str, feature_flags_scope):
    response = feature_flags_scope["admin"].patch(
        f"{base_url}/api/admin/feature-flags/{uuid.uuid4().hex}",
        json={"is_active": True},
        timeout=35,
    )
    assert response.status_code == 404


def test_public_visibility_map_reflects_admin_toggle_changes(base_url: str, feature_flags_scope):
    _set_flag(feature_flags_scope, base_url, "tips", is_active=False, is_visible=False)
    _set_flag(feature_flags_scope, base_url, "visible_badges", is_active=True, is_visible=False)

    client_session = _login(base_url, "cliente@petpasso.com", "Cliente@123")
    try:
        response = client_session.get(f"{base_url}/api/feature-flags/visibility", timeout=35)
    finally:
        client_session.close()

    assert response.status_code == 200, response.text
    flags = response.json().get("flags", {})
    assert isinstance(flags, dict)
    assert bool(flags.get("tips")) is False
    assert bool(flags.get("visible_badges")) is False
    assert "non_existing_feature" not in flags


def test_tips_inactive_returns_safe_empty_summary(base_url: str, feature_flags_scope):
    _set_flag(feature_flags_scope, base_url, "tips", is_active=False, is_visible=False)

    walker_session = _login(base_url, "passeador@petpasso.com", "Passeador@123")
    try:
        response = walker_session.get(f"{base_url}/api/walker/tips/summary", timeout=35)
    finally:
        walker_session.close()

    assert response.status_code == 200, response.text
    payload = response.json()
    assert float(payload.get("today_total", -1)) == 0.0
    assert float(payload.get("month_total", -1)) == 0.0
    assert float(payload.get("historical_total", -1)) == 0.0
    assert payload.get("recent_tips") == []


def test_incentives_inactive_returns_no_impact_summary(base_url: str, feature_flags_scope):
    _set_flag(feature_flags_scope, base_url, "habit_incentive", is_active=False, is_visible=False)

    walker_session = _login(base_url, "passeador@petpasso.com", "Passeador@123")
    try:
        response = walker_session.get(f"{base_url}/api/walker/incentives/summary", timeout=35)
    finally:
        walker_session.close()

    assert response.status_code == 200, response.text
    payload = response.json()
    assert float(payload.get("week_earnings", -1)) == 0.0
    assert float(payload.get("month_earnings", -1)) == 0.0
    assert float(payload.get("historical_earnings", -1)) == 0.0
    messages = payload.get("incentive_messages") or []
    assert any("desativada" in str(message).lower() for message in messages)


def test_badges_and_highlights_hidden_in_walkers_payload(base_url: str, feature_flags_scope):
    _set_flag(feature_flags_scope, base_url, "visible_badges", is_active=True, is_visible=False)
    _set_flag(feature_flags_scope, base_url, "weekly_highlights", is_active=True, is_visible=False)

    client_session = _login(base_url, "cliente@petpasso.com", "Cliente@123")
    try:
        response = client_session.get(f"{base_url}/api/walkers", timeout=35)
    finally:
        client_session.close()

    assert response.status_code == 200, response.text
    walkers = response.json()
    assert walkers
    assert all(str(item.get("public_badge") or "") == "" for item in walkers)
    assert all(str(item.get("highlight_label") or "") == "" for item in walkers)
    assert all(bool(item.get("is_premium_featured", False)) is False for item in walkers)
