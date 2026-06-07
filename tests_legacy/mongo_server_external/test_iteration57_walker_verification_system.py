import os
import uuid
from datetime import date, timedelta

import pytest
import requests
from dotenv import dotenv_values
from pymongo import MongoClient


# Module coverage: walker verification levels/fields, ranking impact (+2/+4), feature-flag behavior, audit trail, and auth playbook critical checks.


def _mongo_db():
    backend_env = dotenv_values("/app/backend/.env")
    mongo_url = os.environ.get("MONGO_URL") or backend_env.get("MONGO_URL")
    db_name = os.environ.get("DB_NAME") or backend_env.get("DB_NAME")
    if not mongo_url or not db_name:
        pytest.skip("MONGO_URL/DB_NAME não configurados")
    client = MongoClient(str(mongo_url).strip().strip('"'))
    return client, client[str(db_name).strip().strip('"')]


def _login(base_url: str, email: str, password: str) -> requests.Session:
    session = requests.Session()
    response = session.post(
        f"{base_url}/api/auth/login",
        json={"email": email, "password": password},
        timeout=35,
    )
    if response.status_code != 200:
        session.close()
        pytest.skip(f"Login indisponível para {email}: {response.status_code}")
    token = (response.json() or {}).get("access_token")
    if not token:
        session.close()
        pytest.skip(f"Token ausente para {email}")
    session.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/json"})
    return session


def _find_date_with_walkers(client: requests.Session, base_url: str) -> tuple[str, list[dict]]:
    for offset in range(1, 15):
        target_date = (date.today() + timedelta(days=offset)).isoformat()
        response = client.get(
            f"{base_url}/api/walkers",
            params={"date": target_date, "duration_minutes": 30, "tipo_passeio": "padrao"},
            timeout=35,
        )
        if response.status_code != 200:
            continue
        rows = response.json() if isinstance(response.json(), list) else []
        if rows:
            return target_date, rows
    pytest.skip("Sem passeadores disponíveis para validação de ranking")


@pytest.fixture()
def iter57_scope(base_url: str):
    admin = _login(base_url, "superadmin@petpasso.com", "SuperAdmin@123")
    client = _login(base_url, "cliente@petpasso.com", "Cliente@123")
    control_walker = _login(base_url, "walker@petpasso.com", "Walker@123")

    flags_resp = admin.get(f"{base_url}/api/admin/feature-flags", timeout=35)
    assert flags_resp.status_code == 200, flags_resp.text
    baseline_flags = {row["feature_name"]: row for row in flags_resp.json()}

    scope = {
        "base_url": base_url,
        "admin": admin,
        "client": client,
        "control_walker": control_walker,
        "baseline_flags": baseline_flags,
    }

    yield scope

    original = baseline_flags.get("walker_verification_enabled")
    if original:
        admin.patch(
            f"{base_url}/api/admin/feature-flags/walker_verification_enabled",
            json={"is_active": bool(original.get("is_active", False)), "is_visible": bool(original.get("is_visible", False))},
            timeout=35,
        )

    admin.close()
    client.close()
    control_walker.close()


def test_walker_contract_exposes_verification_fields(iter57_scope):
    base_url = iter57_scope["base_url"]
    client = iter57_scope["client"]
    _, walkers = _find_date_with_walkers(client, base_url)

    valid_levels = {"NONE", "VERIFIED", "PLUS", "PREMIUM"}
    for row in walkers:
        assert "is_verified" in row
        assert "verification_level" in row
        assert "verification_score_snapshot" in row
        assert row["verification_level"] in valid_levels
        assert isinstance(row.get("verification_score_snapshot"), int)


def test_seeded_verified_plus_premium_appear_in_ranking(iter57_scope):
    base_url = iter57_scope["base_url"]
    client = iter57_scope["client"]
    _, walkers = _find_date_with_walkers(client, base_url)

    by_name = {str(row.get("name") or ""): str(row.get("verification_level") or "NONE") for row in walkers}
    assert any(name.startswith("Vera") and level == "VERIFIED" for name, level in by_name.items())
    assert any(name.startswith("Paulo") and level == "PLUS" for name, level in by_name.items())
    assert any(name.startswith("Priscila") and level == "PREMIUM" for name, level in by_name.items())


def test_ranking_applies_plus_premium_boost_with_low_score_guard(iter57_scope):
    base_url = iter57_scope["base_url"]
    admin = iter57_scope["admin"]
    client = iter57_scope["client"]
    _, walkers_enabled = _find_date_with_walkers(client, base_url)
    enabled_by_id = {str(row.get("id") or ""): row for row in walkers_enabled}

    # Disable temporarily to isolate verification contribution in ranking score.
    disable_resp = admin.patch(
        f"{base_url}/api/admin/feature-flags/walker_verification_enabled",
        json={"is_active": False, "is_visible": False},
        timeout=35,
    )
    assert disable_resp.status_code == 200, disable_resp.text

    _, walkers_disabled = _find_date_with_walkers(client, base_url)
    disabled_by_id = {str(row.get("id") or ""): row for row in walkers_disabled}

    premium_or_plus_with_high_base = [
        row
        for row in walkers_enabled
        if str(row.get("verification_level") or "NONE") in {"PLUS", "PREMIUM"}
        and float(row.get("score_base_component") or 0.0) >= 55.0
    ]
    if not premium_or_plus_with_high_base:
        pytest.skip("Sem passeador PLUS/PREMIUM com score_base >= 55 no dataset atual")

    for enabled_row in premium_or_plus_with_high_base:
        walker_id = str(enabled_row.get("id") or "")
        disabled_row = disabled_by_id.get(walker_id)
        if not disabled_row:
            continue
        enabled_score = float(enabled_row.get("ranking_score_final") or enabled_row.get("match_score") or 0.0)
        disabled_score = float(disabled_row.get("ranking_score_final") or disabled_row.get("match_score") or 0.0)
        assert enabled_score > disabled_score


def test_feature_flag_controls_visibility_and_effect(iter57_scope):
    base_url = iter57_scope["base_url"]
    admin = iter57_scope["admin"]
    client = iter57_scope["client"]

    disable_resp = admin.patch(
        f"{base_url}/api/admin/feature-flags/walker_verification_enabled",
        json={"is_active": False, "is_visible": False},
        timeout=35,
    )
    assert disable_resp.status_code == 200, disable_resp.text

    visibility_resp = client.get(f"{base_url}/api/feature-flags/visibility", timeout=35)
    assert visibility_resp.status_code == 200, visibility_resp.text
    assert (visibility_resp.json() or {}).get("flags", {}).get("walker_verification_enabled") is False

    _, walkers = _find_date_with_walkers(client, base_url)
    for row in walkers:
        assert float(row.get("verification_boost_points") or 0.0) == 0.0
        assert float(row.get("verification_priority_bonus_points") or 0.0) == 0.0
        assert row.get("is_verified") is False
        # Visibility expectation: no verification level should be exposed when feature is disabled.
        assert str(row.get("verification_level") or "NONE") == "NONE"


def test_audit_records_level_change_and_snapshot_on_recalculation(iter57_scope):
    base_url = iter57_scope["base_url"]
    control_walker = iter57_scope["control_walker"]

    me = control_walker.get(f"{base_url}/api/auth/me", timeout=35)
    assert me.status_code == 200, me.text
    walker_id = str((me.json() or {}).get("id") or "")
    assert walker_id

    mongo_client, db = _mongo_db()
    original_user = db.users.find_one(
        {"id": walker_id},
        {
            "_id": 0,
            "perfil_id_verificado": 1,
            "has_water": 1,
            "verification_level": 1,
            "is_verified": 1,
            "verification_score_snapshot": 1,
        },
    )
    assert original_user, "Usuário de controle não encontrado no Mongo"

    before_audit_count = db.walker_verification_audit.count_documents({"walker_user_id": walker_id})
    original_water = bool(original_user.get("has_water", True))

    try:
        db.users.update_one(
            {"id": walker_id},
            {
                "$set": {
                    "perfil_id_verificado": False,
                    "updated_at": (date.today().isoformat()),
                }
            },
        )

        trigger_resp = control_walker.patch(
            f"{base_url}/api/walker/certified-kit",
            json={"has_water": (not original_water)},
            timeout=35,
        )
        assert trigger_resp.status_code == 200, trigger_resp.text

        refreshed = db.users.find_one({"id": walker_id}, {"_id": 0, "verification_level": 1, "verification_score_snapshot": 1})
        assert refreshed
        assert str(refreshed.get("verification_level") or "") == "NONE"
        assert isinstance(refreshed.get("verification_score_snapshot"), int)

        after_audit_count = db.walker_verification_audit.count_documents({"walker_user_id": walker_id})
        assert after_audit_count >= before_audit_count + 1

        latest = db.walker_verification_audit.find_one(
            {"walker_user_id": walker_id},
            sort=[("created_at", -1)],
            projection={"_id": 0, "before_level": 1, "after_level": 1, "verification_score_snapshot": 1, "trigger": 1},
        )
        assert latest
        assert latest.get("after_level") == "NONE"
        assert "verification_score_snapshot" in latest
        assert latest.get("trigger") in {"kit_update", "startup_recalculation", "score_update:admin_scan"}
    finally:
        db.users.update_one(
            {"id": walker_id},
            {
                "$set": {
                    "perfil_id_verificado": original_user.get("perfil_id_verificado", True),
                    "has_water": original_water,
                }
            },
        )
        control_walker.patch(
            f"{base_url}/api/walker/certified-kit",
            json={"has_water": original_water},
            timeout=35,
        )
        mongo_client.close()


def test_auth_playbook_cookie_bcrypt_lockout_and_seed_login(base_url: str):
    login_response = requests.post(
        f"{base_url}/api/auth/login",
        json={"email": "admin@petpasso.com", "password": "Admin@123"},
        headers={"Origin": "https://petpasso-mvp.preview.emergentagent.com"},
        timeout=35,
    )
    assert login_response.status_code == 200, login_response.text
    set_cookie = (login_response.headers.get("set-cookie") or "").lower()
    assert "httponly" in set_cookie

    options_response = requests.options(
        f"{base_url}/api/auth/login",
        headers={
            "Origin": "https://petpasso-mvp.preview.emergentagent.com",
            "Access-Control-Request-Method": "POST",
        },
        timeout=35,
    )
    assert options_response.status_code in (200, 204)
    assert (options_response.headers.get("access-control-allow-credentials") or "").lower() == "true"

    mongo_client, db = _mongo_db()
    try:
        admin_row = db.users.find_one({"email": "admin@petpasso.com"}, {"_id": 0, "password_hash": 1})
        assert admin_row and str(admin_row.get("password_hash") or "").startswith("$2b$")
    finally:
        mongo_client.close()

    attack_email = f"iter57_lockout_{uuid.uuid4().hex[:8]}@petpasso.com"
    register_response = requests.post(
        f"{base_url}/api/auth/register",
        json={
            "full_name": "Iter57 Lockout",
            "email": attack_email,
            "password": "Iter57@123",
            "role": "cliente",
            "accepted_terms": True,
            "accepted_privacy": True,
            "accepted_lgpd": True,
        },
        timeout=35,
    )
    assert register_response.status_code in (200, 201, 409, 429)

    statuses = []
    for _ in range(6):
        bad = requests.post(
            f"{base_url}/api/auth/login",
            json={"email": attack_email, "password": "Wrong@123"},
            timeout=35,
        )
        statuses.append(bad.status_code)
    assert 429 in statuses

    seeded_admin_login = requests.post(
        f"{base_url}/api/auth/login",
        json={"email": "superadmin@petpasso.com", "password": "SuperAdmin@123"},
        timeout=35,
    )
    assert seeded_admin_login.status_code == 200
