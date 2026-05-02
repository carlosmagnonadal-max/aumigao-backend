from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
import pytest
import requests
from dotenv import dotenv_values
from pymongo import MongoClient


# Módulo coberto: motor autônomo de decisão (settings/metrics/audit, matching, proteções CR, feature flags)

SUPERADMIN_CREDS = {"email": "superadmin@petpasso.com", "password": "SuperAdmin@123"}
CLIENT_CREDS = {"email": "cliente@petpasso.com", "password": "Cliente@123"}
WALKER_CREDS = {"email": "walker@petpasso.com", "password": "Walker@123"}

TEST_TAG = "TEST_ITER60"
TEST_CITY = "salvador"
TEST_NEIGHBORHOOD = "teste_bairro_60"


def _mongo_db():
    env = dotenv_values("/app/backend/.env")
    mongo_url = str(env.get("MONGO_URL") or "").strip().strip('"')
    db_name = str(env.get("DB_NAME") or "").strip().strip('"')
    if not mongo_url or not db_name:
        pytest.skip("MONGO_URL/DB_NAME indisponíveis")
    client = MongoClient(mongo_url)
    return client, client[db_name]


def _clear_login_attempts(db, email: str):
    db.login_attempts.delete_many({"identifier": {"$regex": f":{email.lower()}$"}})


def _login(base_url: str, email: str, password: str, db=None) -> requests.Session:
    session = requests.Session()
    if db is not None:
        _clear_login_attempts(db, email)
    response = session.post(
        f"{base_url}/api/auth/login",
        json={"email": email, "password": password},
        timeout=30,
    )
    assert response.status_code == 200, response.text
    token = response.json().get("access_token")
    assert token
    session.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/json"})
    return session


def _make_matching_payload(*, walk_date: str, walk_time: str = "10:30") -> dict[str, Any]:
    return {
        "pet_name": f"{TEST_TAG}_PET",
        "client_name": f"{TEST_TAG}_CLIENT",
        "walk_date": walk_date,
        "walk_time": walk_time,
        "duration_minutes": 30,
        "walk_type": "Individual",
        "modo_inicio_passeio": "endereco_tutor",
        "pickup_street": "Rua Teste 60",
        "pickup_number": "100",
        "pickup_neighborhood": TEST_NEIGHBORHOOD,
        "pickup_complement": "",
        "location_reference": TEST_TAG,
        "pet_behavior_notes": "",
        "notes": f"{TEST_TAG} matching",
    }


def _create_test_walker(db, slug: str, *, risk_flag: bool = False, cr_boost: bool = False) -> dict[str, str]:
    walker_id = str(uuid.uuid4())
    email = f"test_iter60_{slug}@petpasso.com"
    now_dt = datetime.now(timezone.utc)
    now_iso = now_dt.isoformat()
    cr_until = (now_dt + timedelta(hours=24)).isoformat() if cr_boost else None

    user_doc = {
        "id": walker_id,
        "full_name": f"{TEST_TAG}_{slug}",
        "email": email,
        "password_hash": bcrypt.hashpw("TestIter60@123".encode("utf-8"), bcrypt.gensalt()).decode("utf-8"),
        "role": "passeador",
        "isAdmin": False,
        "permissions": {},
        "isActive": True,
        "region": f"{TEST_CITY} - {TEST_NEIGHBORHOOD}",
        "city": TEST_CITY,
        "neighborhood": TEST_NEIGHBORHOOD,
        "quality_status": "ativo",
        "quality_status_reason": f"{TEST_TAG} seeded",
        "availability_days": ["seg", "ter", "qua", "qui", "sex", "sab", "dom"],
        "availability_start_time": "00:00",
        "availability_end_time": "23:59",
        "availability_blocks": [],
        "unavailable_until": None,
        "behavior_risk_flag_active": risk_flag,
        "flag_suspeita_desintermediacao": False,
        "reputation_credits": 200,
        "cr_matching_boost_until": cr_until,
        "cr_matching_boost_points_active": 9.0,
        "cr_early_wave_until": None,
        "cr_visual_highlight_until": None,
        "match_penalty_points": 0.0,
        "match_penalty_until": None,
        "match_rejection_streak": 0,
        "match_cooldown_until": None,
        "created_at": now_iso,
        "updated_at": now_iso,
        "last_active_at": now_iso,
    }
    db.users.update_one({"id": walker_id}, {"$set": user_doc}, upsert=True)
    return {"id": walker_id, "partner_id": f"partner-{walker_id}", "name": user_doc["full_name"]}


def _seed_walk_history(db, walker: dict[str, str], *, good: bool, total: int = 8):
    now = datetime.now(timezone.utc)
    docs: list[dict[str, Any]] = []
    for idx in range(total):
        walk_dt = now - timedelta(days=idx + 1)
        status = "Finalizado" if good else ("Não comparecimento do passeador" if idx < max(2, total // 2) else "Cancelado")
        rating = 5 if good else 1
        docs.append(
            {
                "id": f"{TEST_TAG}_{uuid.uuid4().hex[:10]}",
                "pet_name": f"{TEST_TAG}_PET_{idx}",
                "client_name": f"{TEST_TAG}_CLIENT",
                "walk_type": "Individual",
                "walk_date": walk_dt.strftime("%Y-%m-%d"),
                "walk_time": walk_dt.strftime("%H:%M"),
                "duration_minutes": 30,
                "walker_id": walker["partner_id"],
                "walker_user_id": walker["id"],
                "walker_name": walker["name"],
                "pickup_street": "Rua Teste",
                "pickup_number": "10",
                "pickup_neighborhood": TEST_NEIGHBORHOOD,
                "pickup_complement": "",
                "location_reference": TEST_TAG,
                "security_code": "1234",
                "rating": rating,
                "rating_comment": f"{TEST_TAG}",
                "summary_text": "",
                "pet_behavior_notes": "",
                "notes": f"{TEST_TAG} history",
                "motivoCancelamento": "",
                "tipoCancelamento": None,
                "penalidadePercentual": 0,
                "base_price": 45.0,
                "walker_payout": 33.75,
                "charged_amount": 45.0,
                "walker_payout_amount": 33.75,
                "platform_retained_amount": 11.25,
                "client_refund_amount": 0.0,
                "status": status,
                "walk_datetime_iso": walk_dt.isoformat(),
                "created_at": walk_dt.isoformat(),
                "updated_at": walk_dt.isoformat(),
            }
        )
    if docs:
        db.walks.insert_many(docs)


@pytest.fixture(scope="function")
def marketplace_env(base_url: str):
    mongo_client, db = _mongo_db()
    backup_settings = db.marketplace_intelligence_settings.find_one({"id": "default"}, {"_id": 0})
    backup_flags = {
        name: db.feature_flags.find_one({"feature_name": name}, {"_id": 0})
        for name in ["motor_autonomo_enabled", "cr_system_enabled", "dynamic_adjustment_enabled"]
    }

    def _cleanup():
        db.users.delete_many({"email": {"$regex": r"^test_iter60_.*@petpasso\.com$"}})
        db.walks.delete_many({"$or": [{"pet_name": {"$regex": f"^{TEST_TAG}"}}, {"notes": {"$regex": f"^{TEST_TAG}"}}]})
        db.matching_requests.delete_many({"$or": [{"client_name": {"$regex": f"^{TEST_TAG}"}}, {"notes": {"$regex": f"^{TEST_TAG}"}}]})
        db.walker_requests.delete_many({"$or": [{"client_name": {"$regex": f"^{TEST_TAG}"}}, {"neighborhood": TEST_NEIGHBORHOOD}]})
        db.marketplace_context_snapshots.delete_many({"neighborhood": TEST_NEIGHBORHOOD})
        db.marketplace_decision_audit.delete_many({
            "$or": [
                {"neighborhood": TEST_NEIGHBORHOOD},
                {"city": TEST_CITY},
                {"request_id": {"$in": [row.get("id") for row in db.matching_requests.find({"client_name": {"$regex": f"^{TEST_TAG}"}}, {"_id": 0, "id": 1})]}},
            ]
        })

    _cleanup()

    try:
        yield {"db": db, "base_url": base_url, "backup_settings": backup_settings, "backup_flags": backup_flags}
    finally:
        _cleanup()
        if backup_settings:
            db.marketplace_intelligence_settings.update_one({"id": "default"}, {"$set": backup_settings}, upsert=True)
        for name, value in backup_flags.items():
            if value:
                db.feature_flags.update_one({"feature_name": name}, {"$set": value}, upsert=True)
        mongo_client.close()


def test_admin_marketplace_settings_get_defaults_authorized(marketplace_env):
    db = marketplace_env["db"]
    admin = _login(marketplace_env["base_url"], SUPERADMIN_CREDS["email"], SUPERADMIN_CREDS["password"], db=db)

    resp = admin.get(f"{marketplace_env['base_url']}/api/admin/marketplace-intelligence/settings", timeout=30)
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert data["mode"] == "automatico"
    assert float(data["cr_weight_percent"]) <= 20.0
    assert float(data["critical_ratio_threshold"]) == pytest.approx(1.2, abs=1e-4)
    assert float(data["balanced_ratio_floor"]) == pytest.approx(0.8, abs=1e-4)
    assert float(data["balanced_ratio_ceil"]) == pytest.approx(1.2, abs=1e-4)
    admin.close()


def test_admin_marketplace_settings_patch_persists_and_audits(marketplace_env):
    db = marketplace_env["db"]
    admin = _login(marketplace_env["base_url"], SUPERADMIN_CREDS["email"], SUPERADMIN_CREDS["password"], db=db)

    payload = {
        "critical_ratio_threshold": 1.35,
        "balanced_ratio_floor": 0.82,
        "balanced_ratio_ceil": 1.28,
        "low_supply_wave_extra_candidates": 3,
    }
    patched = admin.patch(
        f"{marketplace_env['base_url']}/api/admin/marketplace-intelligence/settings",
        json=payload,
        timeout=30,
    )
    assert patched.status_code == 200, patched.text
    body = patched.json()

    assert float(body["critical_ratio_threshold"]) == pytest.approx(1.35, abs=1e-4)
    assert float(body["balanced_ratio_floor"]) == pytest.approx(0.82, abs=1e-4)
    assert float(body["balanced_ratio_ceil"]) == pytest.approx(1.28, abs=1e-4)
    assert int(body["low_supply_wave_extra_candidates"]) == 3

    persisted = db.marketplace_intelligence_settings.find_one({"id": "default"}, {"_id": 0})
    assert persisted is not None
    assert float(persisted.get("critical_ratio_threshold", 0)) == pytest.approx(1.35, abs=1e-4)

    audit = db.marketplace_intelligence_settings_audit.find_one({}, {"_id": 0}, sort=[("created_at", -1)])
    assert audit is not None
    assert isinstance(audit.get("actor_admin_id"), str) and audit.get("actor_admin_id")
    assert float(audit.get("changes", {}).get("critical_ratio_threshold", 0)) == pytest.approx(1.35, abs=1e-4)
    admin.close()


def test_marketplace_metrics_returns_context_and_kpis(marketplace_env):
    db = marketplace_env["db"]
    admin = _login(marketplace_env["base_url"], SUPERADMIN_CREDS["email"], SUPERADMIN_CREDS["password"], db=db)

    resp = admin.get(
        f"{marketplace_env['base_url']}/api/admin/marketplace-intelligence/metrics",
        params={"city": TEST_CITY, "neighborhood": TEST_NEIGHBORHOOD},
        timeout=30,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert data["context_state"] in {"critico", "equilibrado", "sobra_oferta"}
    assert isinstance(data["demand_active"], int)
    assert isinstance(data["supply_active"], int)
    assert isinstance(data["demand_supply_ratio"], (int, float))
    assert isinstance(data["match_rate"], (int, float))
    assert isinstance(data["average_acceptance_seconds"], (int, float))
    assert isinstance(data["cancel_rate"], (int, float))
    assert isinstance(data["cr_usage_24h"], int)
    admin.close()


def test_matching_request_applies_engine_and_audit_contains_required_fields(marketplace_env):
    db = marketplace_env["db"]
    admin = _login(marketplace_env["base_url"], SUPERADMIN_CREDS["email"], SUPERADMIN_CREDS["password"], db=db)
    client = _login(marketplace_env["base_url"], CLIENT_CREDS["email"], CLIENT_CREDS["password"], db=db)

    good = _create_test_walker(db, "good", risk_flag=False, cr_boost=False)
    risky = _create_test_walker(db, "risky", risk_flag=True, cr_boost=True)
    low = _create_test_walker(db, "low", risk_flag=False, cr_boost=True)
    _seed_walk_history(db, good, good=True, total=10)
    _seed_walk_history(db, risky, good=True, total=9)
    _seed_walk_history(db, low, good=False, total=9)

    walk_date = (datetime.now(timezone.utc) + timedelta(days=3)).strftime("%Y-%m-%d")
    created = client.post(
        f"{marketplace_env['base_url']}/api/walks/matching-request",
        json=_make_matching_payload(walk_date=walk_date),
        timeout=45,
    )
    assert created.status_code == 201, created.text
    response = created.json()

    assert response["marketplace_context"] in {"critico", "equilibrado", "sobra_oferta"}
    assert isinstance(response["demand_active"], int)
    assert isinstance(response["supply_active"], int)
    assert isinstance(response["demand_supply_ratio"], (int, float))

    matching_row = db.matching_requests.find_one({"id": response["id"]}, {"_id": 0})
    assert matching_row is not None
    assert "marketplace_context" in matching_row
    assert "match_rate" in matching_row
    assert "average_acceptance_seconds" in matching_row
    assert "cancel_rate" in matching_row
    assert "cr_usage_24h" in matching_row

    candidates = list(matching_row.get("candidates") or [])
    assert len(candidates) >= 1

    top = candidates[0]
    top_base = float(top.get("score_base_component", 0.0) or 0.0)
    if top_base < 55.0:
        assert all(float(item.get("score_base_component", 0.0) or 0.0) < 55.0 for item in candidates)

    for item in candidates:
        base_priority = float(item.get("score_base_component", 0.0) or 0.0) + float(item.get("proximity_boost_points", 0.0) or 0.0) + float(item.get("premium_boost_points", 0.0) or 0.0)
        cr_adjusted = float(item.get("cr_boost_adjusted", 0.0) or 0.0)
        assert cr_adjusted <= (max(0.0, base_priority * 0.2) + 0.05)

    risky_item = next((x for x in candidates if x.get("walker_user_id") == risky["id"]), None)
    if risky_item is not None:
        assert float(risky_item.get("cr_boost_raw", 0.0) or 0.0) > 0.0
        assert float(risky_item.get("cr_boost_adjusted", 0.0) or 0.0) == pytest.approx(0.0, abs=1e-6)

    audit_list = admin.get(f"{marketplace_env['base_url']}/api/admin/marketplace-intelligence/audit", params={"limit": 50}, timeout=30)
    assert audit_list.status_code == 200, audit_list.text
    rows = audit_list.json()
    assert isinstance(rows, list) and len(rows) >= 1

    entry = next((row for row in rows if row.get("request_id") == response["id"]), None)
    assert entry is not None
    assert entry["context_state"] in {"critico", "equilibrado", "sobra_oferta"}
    assert isinstance(entry.get("min_score_threshold"), (int, float))
    assert isinstance(entry.get("top_limit"), int)
    assert isinstance(entry.get("selected_candidates_preview"), list)
    if entry["selected_candidates_preview"]:
        preview0 = entry["selected_candidates_preview"][0]
        assert "rank_position" in preview0
        assert "score_base_component" in preview0
        assert "cr_boost_adjusted" in preview0
        assert "context_adjustment_points" in preview0

    client.close()
    admin.close()


def test_feature_flags_exist_and_cr_system_gate_blocks_use_endpoint(marketplace_env):
    db = marketplace_env["db"]
    admin = _login(marketplace_env["base_url"], SUPERADMIN_CREDS["email"], SUPERADMIN_CREDS["password"], db=db)

    flags_resp = admin.get(f"{marketplace_env['base_url']}/api/admin/feature-flags", timeout=30)
    assert flags_resp.status_code == 200, flags_resp.text
    flags = {row["feature_name"]: row for row in flags_resp.json()}

    for feature_name in ["motor_autonomo_enabled", "cr_system_enabled", "dynamic_adjustment_enabled"]:
        assert feature_name in flags
        assert isinstance(flags[feature_name]["is_active"], bool)

    disable = admin.patch(
        f"{marketplace_env['base_url']}/api/admin/feature-flags/cr_system_enabled",
        json={"is_active": False, "is_visible": False},
        timeout=30,
    )
    assert disable.status_code == 200, disable.text
    assert disable.json()["is_active"] is False

    walker = _login(marketplace_env["base_url"], WALKER_CREDS["email"], WALKER_CREDS["password"], db=db)
    use_resp = walker.post(
        f"{marketplace_env['base_url']}/api/walker/reputation-credits/use",
        json={"action": "matching_boost"},
        timeout=30,
    )
    assert use_resp.status_code == 400, use_resp.text
    assert "desativado" in str(use_resp.json().get("detail", "")).lower()

    enable = admin.patch(
        f"{marketplace_env['base_url']}/api/admin/feature-flags/cr_system_enabled",
        json={"is_active": True, "is_visible": True},
        timeout=30,
    )
    assert enable.status_code == 200, enable.text
    assert enable.json()["is_active"] is True

    walker.close()
    admin.close()


def test_auth_security_basics_for_playbook(marketplace_env):
    """Sanity auth checks used by this backend test cycle."""
    db = marketplace_env["db"]

    _clear_login_attempts(db, SUPERADMIN_CREDS["email"])
    login = requests.post(
        f"{marketplace_env['base_url']}/api/auth/login",
        json=SUPERADMIN_CREDS,
        timeout=30,
    )
    assert login.status_code == 200, login.text

    set_cookie = login.headers.get("set-cookie", "")
    assert "access_token=" in set_cookie and "HttpOnly" in set_cookie
    assert "refresh_token=" in set_cookie

    user_row = db.users.find_one({"email": SUPERADMIN_CREDS["email"]}, {"_id": 0, "password_hash": 1})
    assert user_row is not None
    password_hash = str(user_row.get("password_hash") or "")
    assert password_hash.startswith("$2b$")
    assert bcrypt.checkpw(SUPERADMIN_CREDS["password"].encode("utf-8"), password_hash.encode("utf-8"))
