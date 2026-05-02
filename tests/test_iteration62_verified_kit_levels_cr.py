from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import bcrypt
import pytest
import requests
from dotenv import dotenv_values
from pymongo import MongoClient


# Module coverage: certified kit (new fields/upload), two-step checklist gates, kit issue impact,
# premium verified badge eligibility, level settings/feature flags, CR multipliers by level, auth playbook checks.

TEST_TAG = "TEST_ITER62"


def _mongo_db():
    backend_env = dotenv_values("/app/backend/.env")
    mongo_url = os.environ.get("MONGO_URL") or backend_env.get("MONGO_URL")
    db_name = os.environ.get("DB_NAME") or backend_env.get("DB_NAME")
    if not mongo_url or not db_name:
        pytest.skip("MONGO_URL/DB_NAME não configurados")
    client = MongoClient(str(mongo_url).strip().strip('"'))
    return client, client[str(db_name).strip().strip('"')]


def _login(base_url: str, email: str, password: str, *, forwarded_for: str | None = None) -> requests.Session:
    session = requests.Session()
    headers: dict[str, str] = {"Accept": "application/json"}
    if forwarded_for:
        headers["x-forwarded-for"] = forwarded_for
    response = session.post(
        f"{base_url}/api/auth/login",
        json={"email": email, "password": password},
        headers=headers,
        timeout=30,
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


def _create_seed_walk(
    db,
    *,
    walker_user_id: str,
    walker_name: str,
    walker_partner_id: str,
    client_user_id: str,
    client_name: str,
    day_offset: int,
) -> str:
    walk_dt = datetime.now(timezone.utc) + timedelta(days=day_offset)
    walk_id = f"{TEST_TAG}_WALK_{uuid.uuid4().hex[:10]}"
    row = {
        "id": walk_id,
        "pet_name": f"{TEST_TAG}_PET",
        "client_user_id": client_user_id,
        "client_name": client_name,
        "walk_type": "Individual",
        "tipoPasseio": "padrao",
        "modoInicioPasseio": "endereco_tutor",
        "walk_date": walk_dt.strftime("%Y-%m-%d"),
        "walk_time": walk_dt.strftime("%H:%M"),
        "duration_minutes": 30,
        "walker_id": walker_partner_id,
        "walker_user_id": walker_user_id,
        "walker_name": walker_name,
        "pickup_street": "Rua Teste",
        "pickup_number": "100",
        "pickup_neighborhood": "Centro",
        "pickup_complement": "",
        "location_reference": TEST_TAG,
        "security_code": "1234",
        "summary_text": "",
        "pet_behavior_notes": "",
        "notes": TEST_TAG,
        "motivoCancelamento": "",
        "tipoCancelamento": None,
        "penalidadePercentual": 0,
        "base_price": 45.0,
        "walker_payout": 33.75,
        "charged_amount": 45.0,
        "walker_payout_amount": 33.75,
        "platform_retained_amount": 11.25,
        "client_refund_amount": 0.0,
        "status": "Agendado",
        "walk_datetime_iso": walk_dt.isoformat(),
        "created_at": walk_dt.isoformat(),
        "updated_at": walk_dt.isoformat(),
    }
    db.walks.insert_one(row)
    return walk_id


def _kit_checklist_payload() -> dict[str, bool]:
    return {
        "checklist_confirm_water": True,
        "checklist_confirm_bowl": True,
        "checklist_confirm_bags": True,
        "checklist_confirm_first_aid": True,
    }


@pytest.fixture(scope="module")
def scope(base_url: str):
    mongo_client, db = _mongo_db()

    admin = _login(base_url, "superadmin@petpasso.com", "SuperAdmin@123")
    walker = _login(base_url, "walker@petpasso.com", "Walker@123")
    client = _login(base_url, "cliente@petpasso.com", "Cliente@123")

    walker_me = walker.get(f"{base_url}/api/auth/me", timeout=30)
    client_me = client.get(f"{base_url}/api/auth/me", timeout=30)
    assert walker_me.status_code == 200, walker_me.text
    assert client_me.status_code == 200, client_me.text

    walker_user = walker_me.json()
    client_user = client_me.json()

    walker_user_id = str(walker_user.get("id") or "")
    walker_name = str(walker_user.get("full_name") or "")
    client_user_id = str(client_user.get("id") or "")
    client_name = str(client_user.get("full_name") or "")
    assert walker_user_id and walker_name and client_user_id

    original_walker_doc = db.users.find_one({"id": walker_user_id}, {"_id": 0}) or {}
    walker_partner_id = f"partner-{walker_user_id}"

    original_level_settings = admin.get(f"{base_url}/api/admin/walker-level/settings", timeout=30)
    assert original_level_settings.status_code == 200, original_level_settings.text

    payload = {
        "base_url": base_url,
        "db": db,
        "admin": admin,
        "walker": walker,
        "client": client,
        "walker_user_id": walker_user_id,
        "walker_name": walker_name,
        "walker_partner_id": walker_partner_id,
        "client_user_id": client_user_id,
        "client_name": client_name,
        "original_walker_doc": original_walker_doc,
        "original_level_settings": original_level_settings.json(),
    }

    yield payload

    db.walks.delete_many({"notes": TEST_TAG})
    db.walks.delete_many({"id": {"$regex": f"^{TEST_TAG}_"}})
    db.users.delete_many({"email": {"$regex": r"^test_iter62_.*@petpasso\.com$"}})
    db.login_attempts.delete_many({"identifier": {"$regex": r"^198\.51\.100\..*"}})

    restore = dict(payload["original_walker_doc"])
    if restore:
        restore["updated_at"] = datetime.now(timezone.utc).isoformat()
        db.users.update_one({"id": walker_user_id}, {"$set": restore}, upsert=True)

    admin.patch(
        f"{base_url}/api/admin/walker-level/settings",
        json={
            key: value
            for key, value in payload["original_level_settings"].items()
            if key not in {"id", "updated_at", "updated_by"}
        },
        timeout=30,
    )

    admin.close()
    walker.close()
    client.close()
    mongo_client.close()


def test_certified_kit_get_patch_with_new_fields(scope):
    walker = scope["walker"]
    base_url = scope["base_url"]

    original = walker.get(f"{base_url}/api/walker/certified-kit", timeout=30)
    assert original.status_code == 200, original.text
    original_data = original.json()

    try:
        patched = walker.patch(
            f"{base_url}/api/walker/certified-kit",
            json={
                "water_sealed": True,
                "water_bowl": True,
                "poop_bags": True,
                "first_aid_kit": True,
            },
            timeout=30,
        )
        assert patched.status_code == 200, patched.text
        data = patched.json()
        assert data["water_sealed"] is True
        assert data["water_bowl"] is True
        assert data["poop_bags"] is True
        assert data["first_aid_kit"] is True
        assert data["kit_complete"] is True
        assert data["walker_kit"]["kit_complete"] is True

        persisted = walker.get(f"{base_url}/api/walker/certified-kit", timeout=30)
        assert persisted.status_code == 200, persisted.text
        persisted_data = persisted.json()
        assert persisted_data["kit_complete"] is True
    finally:
        walker.patch(
            f"{base_url}/api/walker/certified-kit",
            json={
                "water_sealed": original_data.get("water_sealed", original_data.get("has_water", False)),
                "water_bowl": original_data.get("water_bowl", original_data.get("has_bowl", False)),
                "poop_bags": original_data.get("poop_bags", original_data.get("has_bags", False)),
                "first_aid_kit": original_data.get("first_aid_kit", original_data.get("has_first_aid", False)),
                "kit_photo_urls": original_data.get("kit_photo_urls", []),
            },
            timeout=30,
        )


def test_upload_photo_limits_to_three_and_returns_upload_urls(scope):
    walker = scope["walker"]
    base_url = scope["base_url"]

    before = walker.get(f"{base_url}/api/walker/certified-kit", timeout=30)
    assert before.status_code == 200, before.text
    original_urls = list((before.json() or {}).get("kit_photo_urls") or [])

    walker.patch(f"{base_url}/api/walker/certified-kit", json={"kit_photo_urls": []}, timeout=30)

    try:
        for idx in range(1, 4):
            upload = walker.post(
                f"{base_url}/api/walker/certified-kit/upload-photo",
                files={"file": (f"kit_{idx}.jpg", b"\xff\xd8\xff\xe0" + bytes([idx]) * 100, "image/jpeg")},
                timeout=30,
            )
            assert upload.status_code == 200, upload.text
            urls = (upload.json() or {}).get("kit_photo_urls") or []
            assert len(urls) == idx
            assert str(urls[-1]).startswith("/uploads/")

        blocked = walker.post(
            f"{base_url}/api/walker/certified-kit/upload-photo",
            files={"file": ("kit_4.jpg", b"\xff\xd8\xff\xe0" + b"x" * 100, "image/jpeg")},
            timeout=30,
        )
        assert blocked.status_code == 400
        assert "Limite de 3 fotos" in blocked.text
    finally:
        walker.patch(
            f"{base_url}/api/walker/certified-kit",
            json={"kit_photo_urls": original_urls},
            timeout=30,
        )


def test_two_step_checklist_blocks_and_then_allows_start_handover(scope):
    db = scope["db"]
    base_url = scope["base_url"]
    walker = scope["walker"]
    client = scope["client"]

    walk_id = _create_seed_walk(
        db,
        walker_user_id=scope["walker_user_id"],
        walker_name=scope["walker_name"],
        walker_partner_id=scope["walker_partner_id"],
        client_user_id=scope["client_user_id"],
        client_name=scope["client_name"],
        day_offset=2,
    )

    check_in = walker.post(
        f"{base_url}/api/walks/{walk_id}/check-in",
        json=_kit_checklist_payload(),
        timeout=30,
    )
    assert check_in.status_code == 200, check_in.text

    start_blocked = walker.post(
        f"{base_url}/api/walks/{walk_id}/kit-checklist/start",
        json=_kit_checklist_payload(),
        timeout=30,
    )
    assert start_blocked.status_code == 400
    assert "Checklist de chegada" in start_blocked.text

    handover_blocked = client.post(f"{base_url}/api/walks/{walk_id}/confirm-handover", timeout=30)
    assert handover_blocked.status_code == 400
    assert "Checklist de chegada pendente" in handover_blocked.text

    validate = client.post(
        f"{base_url}/api/walks/{walk_id}/kit-checklist/check-in-validate",
        json=_kit_checklist_payload(),
        timeout=30,
    )
    assert validate.status_code == 200, validate.text
    assert validate.json().get("kit_checklist_check_in_confirmed") is True

    persisted_walk = db.walks.find_one({"id": walk_id}, {"_id": 0, "checklist_validado_chegada": 1}) or {}
    assert bool(persisted_walk.get("checklist_validado_chegada", False)) is True

    start_ok = walker.post(
        f"{base_url}/api/walks/{walk_id}/kit-checklist/start",
        json=_kit_checklist_payload(),
        timeout=30,
    )
    assert start_ok.status_code == 200, start_ok.text
    assert start_ok.json().get("kit_checklist_start_confirmed") is True

    persisted_walk_after_start = db.walks.find_one({"id": walk_id}, {"_id": 0, "checklist_confirmado_inicio": 1}) or {}
    assert bool(persisted_walk_after_start.get("checklist_confirmado_inicio", False)) is True

    handover_ok = client.post(f"{base_url}/api/walks/{walk_id}/confirm-handover", timeout=30)
    assert handover_ok.status_code == 200, handover_ok.text
    assert handover_ok.json().get("status") in {"Passeando agora", "Finalizado"}


def test_kit_issue_report_applies_progressive_impact_and_resets_streak(scope):
    db = scope["db"]
    base_url = scope["base_url"]
    client = scope["client"]
    walker_user_id = scope["walker_user_id"]

    db.users.update_one(
        {"id": walker_user_id},
        {"$set": {"kit_missing_reports_count": 0, "kit_reliability_penalty_points": 0.0, "kit_checklist_streak": 7}},
    )

    walk1 = _create_seed_walk(
        db,
        walker_user_id=walker_user_id,
        walker_name=scope["walker_name"],
        walker_partner_id=scope["walker_partner_id"],
        client_user_id=scope["client_user_id"],
        client_name=scope["client_name"],
        day_offset=3,
    )
    walk2 = _create_seed_walk(
        db,
        walker_user_id=walker_user_id,
        walker_name=scope["walker_name"],
        walker_partner_id=scope["walker_partner_id"],
        client_user_id=scope["client_user_id"],
        client_name=scope["client_name"],
        day_offset=4,
    )

    report1 = client.post(
        f"{base_url}/api/walks/{walk1}/kit-issue-report",
        json={"confirm_report": True, "missing_items": ["has_water"], "note": f"{TEST_TAG} issue 1"},
        timeout=30,
    )
    assert report1.status_code == 200, report1.text

    walker_after_first = db.users.find_one(
        {"id": walker_user_id},
        {"_id": 0, "kit_missing_reports_count": 1, "kit_reliability_penalty_points": 1, "kit_checklist_streak": 1},
    )
    assert int((walker_after_first or {}).get("kit_missing_reports_count", 0)) == 1
    assert int((walker_after_first or {}).get("kit_checklist_streak", 0)) == 0

    report2 = client.post(
        f"{base_url}/api/walks/{walk2}/kit-issue-report",
        json={"confirm_report": True, "missing_items": ["has_bowl"], "note": f"{TEST_TAG} issue 2"},
        timeout=30,
    )
    assert report2.status_code == 200, report2.text

    walker_after_second = db.users.find_one(
        {"id": walker_user_id},
        {"_id": 0, "kit_missing_reports_count": 1, "kit_reliability_penalty_points": 1},
    )
    assert int((walker_after_second or {}).get("kit_missing_reports_count", 0)) == 2
    assert float((walker_after_second or {}).get("kit_reliability_penalty_points", 0.0)) >= 2.5


def test_premium_badge_requires_two_step_checklist_kit_complete_and_no_active_infraction(scope):
    db = scope["db"]
    base_url = scope["base_url"]
    walker = scope["walker"]
    client = scope["client"]
    walker_user_id = scope["walker_user_id"]

    db.users.update_one(
        {"id": walker_user_id},
        {
            "$set": {
                "water_sealed": True,
                "water_bowl": True,
                "poop_bags": True,
                "first_aid_kit": True,
                "has_water": True,
                "has_bowl": True,
                "has_bags": True,
                "has_first_aid": True,
                "premium_verified_streak": 4,
                "premium_verified_badge_active": False,
                "premium_verified_infractions_consecutive": 0,
            }
        },
    )

    walk_ok = _create_seed_walk(
        db,
        walker_user_id=walker_user_id,
        walker_name=scope["walker_name"],
        walker_partner_id=scope["walker_partner_id"],
        client_user_id=scope["client_user_id"],
        client_name=scope["client_name"],
        day_offset=5,
    )

    assert walker.post(f"{base_url}/api/walks/{walk_ok}/check-in", json=_kit_checklist_payload(), timeout=30).status_code == 200
    assert client.post(
        f"{base_url}/api/walks/{walk_ok}/kit-checklist/check-in-validate",
        json=_kit_checklist_payload(),
        timeout=30,
    ).status_code == 200
    assert walker.post(
        f"{base_url}/api/walks/{walk_ok}/kit-checklist/start",
        json=_kit_checklist_payload(),
        timeout=30,
    ).status_code == 200

    status_after_ok = walker.get(f"{base_url}/api/walker/premium-verified-status", timeout=30)
    assert status_after_ok.status_code == 200, status_after_ok.text
    assert status_after_ok.json().get("badge_active") is True

    walk_bad = _create_seed_walk(
        db,
        walker_user_id=walker_user_id,
        walker_name=scope["walker_name"],
        walker_partner_id=scope["walker_partner_id"],
        client_user_id=scope["client_user_id"],
        client_name=scope["client_name"],
        day_offset=6,
    )
    issue = client.post(
        f"{base_url}/api/walks/{walk_bad}/kit-issue-report",
        json={"confirm_report": True, "missing_items": ["first_aid_kit"], "note": f"{TEST_TAG} severe infraction"},
        timeout=30,
    )
    assert issue.status_code == 200, issue.text

    status_after_issue = walker.get(f"{base_url}/api/walker/premium-verified-status", timeout=30)
    assert status_after_issue.status_code == 200, status_after_issue.text
    assert status_after_issue.json().get("badge_active") is False


def test_feature_flags_expose_new_kit_premium_level_flags(scope):
    admin = scope["admin"]
    client = scope["client"]
    base_url = scope["base_url"]

    admin_flags = admin.get(f"{base_url}/api/admin/feature-flags", timeout=30)
    assert admin_flags.status_code == 200, admin_flags.text
    by_name = {row["feature_name"]: row for row in (admin_flags.json() or [])}
    for flag_name in ["kit_system_enabled", "premium_verified_enabled", "level_system_enabled"]:
        assert flag_name in by_name
        assert isinstance(by_name[flag_name].get("is_active"), bool)

    visible = client.get(f"{base_url}/api/feature-flags/visibility", timeout=30)
    assert visible.status_code == 200, visible.text
    flags = (visible.json() or {}).get("flags") or {}
    for flag_name in ["kit_system_enabled", "premium_verified_enabled", "level_system_enabled"]:
        assert flag_name in flags


def test_admin_walker_level_settings_get_patch_persists(scope):
    admin = scope["admin"]
    base_url = scope["base_url"]

    current = admin.get(f"{base_url}/api/admin/walker-level/settings", timeout=30)
    assert current.status_code == 200, current.text
    current_data = current.json()

    new_walks = int(current_data.get("silver_min_walks", 10)) + 1
    if new_walks > 200:
        new_walks = 10

    patched = admin.patch(
        f"{base_url}/api/admin/walker-level/settings",
        json={"silver_min_walks": new_walks},
        timeout=30,
    )
    assert patched.status_code == 200, patched.text
    assert int((patched.json() or {}).get("silver_min_walks", 0)) == new_walks

    persisted = admin.get(f"{base_url}/api/admin/walker-level/settings", timeout=30)
    assert persisted.status_code == 200, persisted.text
    assert int((persisted.json() or {}).get("silver_min_walks", 0)) == new_walks


def test_level_data_on_ranking_and_cr_multipliers_for_silver_gold(scope):
    db = scope["db"]
    base_url = scope["base_url"]
    client_session = scope["client"]

    walkers_resp = client_session.get(
        f"{base_url}/api/walkers",
        params={
            "date": (datetime.now(timezone.utc) + timedelta(days=2)).strftime("%Y-%m-%d"),
            "duration_minutes": 30,
            "tipo_passeio": "padrao",
        },
        timeout=35,
    )
    assert walkers_resp.status_code == 200, walkers_resp.text
    walkers = walkers_resp.json() if isinstance(walkers_resp.json(), list) else []
    assert len(walkers) > 0

    for row in walkers[:8]:
        assert row.get("walker_level") in {"bronze", "silver", "gold", "prata", "ouro", "elite"}
        bonus = float(row.get("level_priority_bonus", 0.0) or 0.0)
        assert 0.0 <= bonus <= 0.06

    test_email = f"test_iter62_cr_{uuid.uuid4().hex[:8]}@petpasso.com"
    test_password = "TestIter62@123"
    now_iso = datetime.now(timezone.utc).isoformat()
    test_user_id = str(uuid.uuid4())
    db.users.insert_one(
        {
            "id": test_user_id,
            "full_name": f"{TEST_TAG} CR Walker",
            "email": test_email,
            "password_hash": bcrypt.hashpw(test_password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8"),
            "role": "passeador",
            "isAdmin": False,
            "permissions": {},
            "isActive": True,
            "walker_level": "silver",
            "verification_level": "NONE",
            "reputation_credits": 100,
            "cr_daily_uses_count": 0,
            "cr_daily_uses_date": "",
            "quality_metrics": {"score_final": 85.0},
            "availability_days": ["seg", "ter", "qua", "qui", "sex", "sab", "dom"],
            "availability_start_time": "08:00",
            "availability_end_time": "18:00",
            "created_at": now_iso,
            "updated_at": now_iso,
            "last_active_at": now_iso,
        }
    )

    silver = _login(base_url, test_email, test_password)
    try:
        silver_credits = silver.get(f"{base_url}/api/walker/reputation-credits", timeout=30)
        assert silver_credits.status_code == 200, silver_credits.text
        silver_data = silver_credits.json()
        assert float(silver_data.get("premium_cost_multiplier", 0)) == pytest.approx(0.9, abs=1e-3)
        assert float(silver_data.get("premium_effect_multiplier", 0)) == pytest.approx(1.0, abs=1e-3)

        silver_use = silver.post(
            f"{base_url}/api/walker/reputation-credits/use",
            json={"action": "matching_boost"},
            timeout=30,
        )
        assert silver_use.status_code == 200, silver_use.text
        assert int((silver_use.json() or {}).get("reputation_credits", 0)) == 96

        db.users.update_one(
            {"id": test_user_id},
            {
                "$set": {
                    "walker_level": "gold",
                    "verification_level": "PREMIUM",
                    "cr_matching_boost_until": None,
                    "cr_early_wave_until": None,
                    "cr_visual_highlight_until": None,
                    "cr_daily_uses_count": 0,
                    "cr_daily_uses_date": "",
                }
            },
        )
    finally:
        silver.close()

    gold = _login(base_url, test_email, test_password)
    try:
        gold_credits = gold.get(f"{base_url}/api/walker/reputation-credits", timeout=30)
        assert gold_credits.status_code == 200, gold_credits.text
        gold_data = gold_credits.json()
        assert float(gold_data.get("premium_cost_multiplier", 0)) == pytest.approx(0.68, abs=1e-3)
        assert float(gold_data.get("premium_effect_multiplier", 0)) == pytest.approx(1.38, abs=1e-3)

        gold_use = gold.post(
            f"{base_url}/api/walker/reputation-credits/use",
            json={"action": "early_wave"},
            timeout=30,
        )
        assert gold_use.status_code == 200, gold_use.text
        gold_body = gold_use.json()
        assert int(gold_body.get("reputation_credits", 0)) == 93
        assert bool(gold_body.get("is_early_wave_active")) is True
    finally:
        gold.close()


def test_auth_playbook_runtime_checks(scope):
    db = scope["db"]
    base_url = scope["base_url"]

    login = requests.post(
        f"{base_url}/api/auth/login",
        json={"email": "superadmin@petpasso.com", "password": "SuperAdmin@123"},
        timeout=30,
    )
    assert login.status_code == 200, login.text
    set_cookie = login.headers.get("set-cookie", "")
    assert "access_token=" in set_cookie and "HttpOnly" in set_cookie
    assert "refresh_token=" in set_cookie

    row = db.users.find_one({"email": "superadmin@petpasso.com"}, {"_id": 0, "password_hash": 1})
    assert row is not None
    assert str(row.get("password_hash") or "").startswith("$2b$")

    origin = base_url.rstrip("/")
    cors_preflight = requests.options(
        f"{base_url}/api/auth/login",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
        timeout=30,
    )
    assert cors_preflight.status_code in {200, 204}
    assert cors_preflight.headers.get("access-control-allow-credentials") == "true"
    assert cors_preflight.headers.get("access-control-allow-origin") == origin

    brute_force_email = "superadmin@petpasso.com"
    brute_ip = f"198.51.100.{int(uuid.uuid4().hex[:2], 16)}"
    identifier = f"{brute_ip}:{brute_force_email}"
    db.login_attempts.delete_many({"identifier": identifier})

    statuses: list[int] = []
    for _ in range(6):
        resp = requests.post(
            f"{base_url}/api/auth/login",
            json={"email": brute_force_email, "password": "WrongPassword@123"},
            headers={"x-forwarded-for": brute_ip},
            timeout=30,
        )
        statuses.append(resp.status_code)

    assert statuses[:5] == [401, 401, 401, 401, 401]
    assert statuses[5] == 429

    db.login_attempts.delete_many({"identifier": identifier})
