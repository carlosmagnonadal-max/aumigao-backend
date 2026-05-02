import os
import uuid
from datetime import datetime, timedelta

import pytest
import requests
from dotenv import dotenv_values
from pymongo import MongoClient


# Module coverage: premium verified feature flags/settings/status, checklist integration, progressive penalty, recovery, and ranking caps.


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
    if response.status_code != 200:
        session.close()
        pytest.skip(f"Login indisponível para {email}: {response.status_code}")
    token = (response.json() or {}).get("access_token")
    if not token:
        session.close()
        pytest.skip(f"Token ausente para {email}")
    session.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/json"})
    return session


def _set_feature_flag(admin: requests.Session, base_url: str, name: str, is_active: bool, is_visible: bool):
    response = admin.patch(
        f"{base_url}/api/admin/feature-flags/{name}",
        json={"is_active": is_active, "is_visible": is_visible},
        timeout=35,
    )
    assert response.status_code == 200, response.text


def _ensure_premium_verified_enabled(scope):
    _set_feature_flag(scope["admin"], scope["base_url"], "premium_verified_badge_enabled", True, True)
    _set_feature_flag(scope["admin"], scope["base_url"], "premium_verified_bonus_enabled", True, True)


def _find_next_slot(session: requests.Session, base_url: str, walker_id: str, start_offset_days: int = 1) -> tuple[str, str, int]:
    today = datetime.utcnow().date()
    for offset in range(start_offset_days, start_offset_days + 25):
        date_str = (today + timedelta(days=offset)).strftime("%Y-%m-%d")
        slots_resp = session.get(
            f"{base_url}/api/walkers/{walker_id}/availability-slots",
            params={"date": date_str, "duration_minutes": 30},
            timeout=35,
        )
        if slots_resp.status_code != 200:
            continue
        slots = (slots_resp.json() or {}).get("available_slots") or []
        if slots:
            return date_str, str(slots[0]), offset
    pytest.skip("Sem horários disponíveis para criar passeios de teste")


def _create_walk_for_walker(client: requests.Session, base_url: str, walker_id: str, date_str: str, time_str: str) -> str:
    marker = f"TEST_ITER54_{uuid.uuid4().hex[:8]}"
    response = client.post(
        f"{base_url}/api/walks",
        json={
            "pet_name": f"{marker}_PET",
            "client_name": "TEST Iter54 Cliente",
            "walk_date": date_str,
            "walk_time": time_str,
            "duration_minutes": 30,
            "walker_id": walker_id,
            "pickup_street": "Rua Teste",
            "pickup_number": "100",
            "pickup_neighborhood": "Centro",
            "pickup_complement": "",
            "location_reference": "TEST",
            "pet_behavior_notes": "TEST",
            "notes": marker,
            "walk_type": "Individual",
            "tipo_passeio": "padrao",
            "modo_inicio_passeio": "endereco_tutor",
        },
        timeout=35,
    )
    assert response.status_code == 201, response.text
    walk_id = str((response.json() or {}).get("id") or "")
    assert walk_id
    return walk_id


def _check_in_and_start_checklist(walker: requests.Session, base_url: str, walk_id: str):
    payload = {
        "checklist_confirm_water": True,
        "checklist_confirm_bowl": True,
        "checklist_confirm_bags": True,
    }
    check_in = walker.post(f"{base_url}/api/walks/{walk_id}/check-in", json=payload, timeout=35)
    assert check_in.status_code == 200, check_in.text

    start = walker.post(f"{base_url}/api/walks/{walk_id}/kit-checklist/start", json=payload, timeout=35)
    assert start.status_code == 200, start.text


def _report_kit_issue(client: requests.Session, base_url: str, walk_id: str, missing_items: list[str]):
    response = client.post(
        f"{base_url}/api/walks/{walk_id}/kit-issue-report",
        json={
            "confirm_report": True,
            "missing_items": missing_items,
            "note": "TEST it54 ocorrência de kit",
        },
        timeout=35,
    )
    assert response.status_code == 200, response.text


def _walker_status(walker: requests.Session, base_url: str) -> dict:
    response = walker.get(f"{base_url}/api/walker/premium-verified-status", timeout=35)
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture()
def premium_verified_scope(base_url: str):
    admin = _login(base_url, "superadmin@petpasso.com", "SuperAdmin@123")
    walker = _login(base_url, "passeador@petpasso.com", "Passeador@123")
    client = _login(base_url, "cliente@petpasso.com", "Cliente@123")

    me_resp = walker.get(f"{base_url}/api/auth/me", timeout=35)
    assert me_resp.status_code == 200, me_resp.text
    walker_user = me_resp.json()
    walker_user_id = str(walker_user.get("id") or "")
    assert walker_user_id

    tasks_resp = walker.get(f"{base_url}/api/walker/tasks", timeout=35)
    assert tasks_resp.status_code == 200, tasks_resp.text
    tasks = tasks_resp.json() if isinstance(tasks_resp.json(), list) else []
    walker_id_for_create = str((tasks[0] if tasks else {}).get("walker_id") or "")
    if not walker_id_for_create:
        pytest.skip("Não foi possível resolver walker_id a partir das tarefas do passeador")

    flags_resp = admin.get(f"{base_url}/api/admin/feature-flags", timeout=35)
    assert flags_resp.status_code == 200, flags_resp.text
    baseline_flags = {row["feature_name"]: row for row in flags_resp.json()}

    settings_resp = admin.get(f"{base_url}/api/admin/premium-verified/settings", timeout=35)
    assert settings_resp.status_code == 200, settings_resp.text
    baseline_settings = settings_resp.json()

    mongo_client, db = _mongo_db()
    walker_row = db.users.find_one(
        {"id": walker_user_id},
        {
            "_id": 0,
            "premium_verified_badge_active": 1,
            "premium_verified_streak": 1,
            "premium_verified_infractions_consecutive": 1,
            "premium_verified_last_reason": 1,
            "premium_verified_penalty_level": 1,
            "premium_verified_penalty_points": 1,
            "premium_verified_level_downgrade": 1,
        },
    )

    scope = {
        "base_url": base_url,
        "admin": admin,
        "walker": walker,
        "client": client,
        "walker_user_id": walker_user_id,
        "walker_id_for_create": walker_id_for_create,
        "baseline_flags": baseline_flags,
        "baseline_settings": baseline_settings,
        "baseline_walker": walker_row or {},
    }

    _ensure_premium_verified_enabled(scope)
    reset_status = {
        "premium_verified_badge_active": False,
        "premium_verified_streak": 0,
        "premium_verified_infractions_consecutive": 0,
        "premium_verified_last_reason": "",
        "premium_verified_penalty_level": "none",
        "premium_verified_penalty_points": 0.0,
        "premium_verified_level_downgrade": 0,
        "updated_at": datetime.utcnow().isoformat(),
    }
    db.users.update_one({"id": walker_user_id}, {"$set": reset_status})

    yield scope

    for feature_name in ["premium_verified_badge_enabled", "premium_verified_bonus_enabled"]:
        original = baseline_flags.get(feature_name)
        if original:
            admin.patch(
                f"{base_url}/api/admin/feature-flags/{feature_name}",
                json={"is_active": bool(original.get("is_active", False)), "is_visible": bool(original.get("is_visible", False))},
                timeout=35,
            )

    admin.patch(
        f"{base_url}/api/admin/premium-verified/settings",
        json={
            "streak_minimo_para_selo": baseline_settings.get("streak_minimo_para_selo"),
            "bonus_score_base": baseline_settings.get("bonus_score_base"),
            "priority_bonus": baseline_settings.get("priority_bonus"),
            "cr_efficiency_multiplier": baseline_settings.get("cr_efficiency_multiplier"),
        },
        timeout=35,
    )

    restore = dict(scope["baseline_walker"])
    if restore:
        restore["updated_at"] = datetime.utcnow().isoformat()
        db.users.update_one({"id": walker_user_id}, {"$set": restore})

    db.walks.delete_many({"notes": {"$regex": "^TEST_ITER54_"}})
    db.payments.delete_many({"notes": {"$regex": "^TEST_ITER54_"}})
    db.walk_matching_requests.delete_many({"notes": {"$regex": "^TEST_ITER54_"}})

    mongo_client.close()
    admin.close()
    walker.close()
    client.close()


def test_feature_flags_include_premium_verified_entries(premium_verified_scope):
    base_url = premium_verified_scope["base_url"]
    admin = premium_verified_scope["admin"]
    client = premium_verified_scope["client"]

    flags_resp = admin.get(f"{base_url}/api/admin/feature-flags", timeout=35)
    assert flags_resp.status_code == 200, flags_resp.text
    by_name = {row["feature_name"]: row for row in flags_resp.json()}
    assert "premium_verified_badge_enabled" in by_name
    assert "premium_verified_bonus_enabled" in by_name

    vis_resp = client.get(f"{base_url}/api/feature-flags/visibility", timeout=35)
    assert vis_resp.status_code == 200, vis_resp.text
    flags = (vis_resp.json() or {}).get("flags") or {}
    assert "premium_verified_badge_enabled" in flags
    assert "premium_verified_bonus_enabled" in flags


def test_admin_settings_get_patch_persists_values(premium_verified_scope):
    base_url = premium_verified_scope["base_url"]
    admin = premium_verified_scope["admin"]

    get_before = admin.get(f"{base_url}/api/admin/premium-verified/settings", timeout=35)
    assert get_before.status_code == 200, get_before.text

    patched = admin.patch(
        f"{base_url}/api/admin/premium-verified/settings",
        json={
            "streak_minimo_para_selo": 3,
            "bonus_score_base": 7.0,
            "priority_bonus": 1.7,
            "cr_efficiency_multiplier": 1.2,
        },
        timeout=35,
    )
    assert patched.status_code == 200, patched.text
    patched_data = patched.json()
    assert patched_data["streak_minimo_para_selo"] == 3
    assert float(patched_data["bonus_score_base"]) == 7.0
    assert float(patched_data["priority_bonus"]) == 1.7
    assert float(patched_data["cr_efficiency_multiplier"]) == 1.2

    get_after = admin.get(f"{base_url}/api/admin/premium-verified/settings", timeout=35)
    assert get_after.status_code == 200, get_after.text
    persisted = get_after.json()
    assert persisted["streak_minimo_para_selo"] == 3
    assert float(persisted["bonus_score_base"]) == 7.0


def test_walker_status_endpoint_returns_reason_and_progress(premium_verified_scope):
    base_url = premium_verified_scope["base_url"]
    walker = premium_verified_scope["walker"]

    payload = _walker_status(walker, base_url)
    assert "badge_active" in payload
    assert "reason" in payload
    assert "progresso" in payload
    assert "infracoes_consecutivas" in payload
    assert "penalty_level" in payload


def test_progressive_loss_and_recovery_with_checklist_integration(premium_verified_scope):
    base_url = premium_verified_scope["base_url"]
    admin = premium_verified_scope["admin"]
    walker = premium_verified_scope["walker"]
    client = premium_verified_scope["client"]
    walker_id = premium_verified_scope["walker_id_for_create"]

    set_resp = admin.patch(
        f"{base_url}/api/admin/premium-verified/settings",
        json={
            "streak_minimo_para_selo": 3,
            "bonus_score_base": 7.0,
            "priority_bonus": 1.5,
            "cr_efficiency_multiplier": 1.2,
        },
        timeout=35,
    )
    assert set_resp.status_code == 200, set_resp.text

    next_offset = 1
    for _ in range(3):
        date_str, time_str, used_offset = _find_next_slot(client, base_url, walker_id, start_offset_days=next_offset)
        walk_id = _create_walk_for_walker(client, base_url, walker_id, date_str, time_str)
        _check_in_and_start_checklist(walker, base_url, walk_id)
        next_offset = used_offset + 1

    active_status = _walker_status(walker, base_url)
    assert active_status["badge_active"] is True
    assert active_status["progresso"] == "3/3"

    date_str, time_str, used_offset = _find_next_slot(client, base_url, walker_id, start_offset_days=next_offset)
    walk1 = _create_walk_for_walker(client, base_url, walker_id, date_str, time_str)
    _report_kit_issue(client, base_url, walk1, ["has_water"])
    st1 = _walker_status(walker, base_url)
    assert st1["badge_active"] is True
    assert int(st1["infracoes_consecutivas"]) >= 1
    assert st1["penalty_level"] == "leve"
    next_offset = used_offset + 1

    date_str, time_str, used_offset = _find_next_slot(client, base_url, walker_id, start_offset_days=next_offset)
    walk2 = _create_walk_for_walker(client, base_url, walker_id, date_str, time_str)
    _report_kit_issue(client, base_url, walk2, ["has_water"])
    st2 = _walker_status(walker, base_url)
    assert st2["badge_active"] is False
    assert int(st2["infracoes_consecutivas"]) >= 2
    assert st2["penalty_level"] in {"moderada", "grave"}
    next_offset = used_offset + 1

    date_str, time_str, used_offset = _find_next_slot(client, base_url, walker_id, start_offset_days=next_offset)
    walk3 = _create_walk_for_walker(client, base_url, walker_id, date_str, time_str)
    _report_kit_issue(client, base_url, walk3, ["has_first_aid"])
    st3 = _walker_status(walker, base_url)
    assert st3["badge_active"] is False
    assert st3["penalty_level"] == "grave"
    next_offset = used_offset + 1

    for _ in range(3):
        date_str, time_str, used_offset = _find_next_slot(client, base_url, walker_id, start_offset_days=next_offset)
        walk_id = _create_walk_for_walker(client, base_url, walker_id, date_str, time_str)
        _check_in_and_start_checklist(walker, base_url, walk_id)
        next_offset = used_offset + 1

    recovered = _walker_status(walker, base_url)
    assert recovered["badge_active"] is True
    assert recovered["progresso"] == "3/3"
    assert int(recovered["infracoes_consecutivas"]) == 0


def test_ranking_applies_caps_and_score_floor_for_badge_bonus(premium_verified_scope):
    base_url = premium_verified_scope["base_url"]
    client = premium_verified_scope["client"]

    query_date = (datetime.utcnow().date() + timedelta(days=2)).strftime("%Y-%m-%d")
    response = client.get(
        f"{base_url}/api/walkers",
        params={"date": query_date, "duration_minutes": 30, "tipo_passeio": "padrao"},
        timeout=35,
    )
    assert response.status_code == 200, response.text
    walkers = response.json() if isinstance(response.json(), list) else []
    assert walkers

    for walker in walkers:
        score_base = float(walker.get("score_base_component") or 0.0)
        bonus_score = float(walker.get("premium_verified_bonus_score_applied") or 0.0)
        bonus_priority = float(walker.get("premium_verified_priority_bonus_applied") or 0.0)
        premium_boost_points = float(walker.get("premium_boost_points") or 0.0)
        assert premium_boost_points <= 12.0

        if bool(walker.get("premium_verified_badge_active")) and score_base < 55.0:
            assert bonus_score == 0.0
            assert bonus_priority == 0.0
