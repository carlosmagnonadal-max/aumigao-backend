from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
import pytest
import requests
from dotenv import dotenv_values
from pymongo import MongoClient


# Module coverage: governança automática de qualidade + recuperação estruturada + visibilidade pública

ADMIN_CREDS = {"email": "admin@petpasso.com", "password": "Admin@123"}
CLIENT_CREDS = {"email": "cliente@petpasso.com", "password": "Cliente@123"}
TEST_WALKER_EMAIL = "test_iter28_walker@petpasso.com"
TEST_WALKER_PASSWORD = "TestIter28@123"
TEST_WALKER_NAME = "TEST ITER28 Walker"


def _mongo_collection(name: str):
    env = dotenv_values("/app/backend/.env")
    mongo_url = str(env.get("MONGO_URL") or "").strip().strip('"')
    db_name = str(env.get("DB_NAME") or "").strip().strip('"')
    if not mongo_url or not db_name:
        pytest.skip("MONGO_URL/DB_NAME indisponíveis para testes de governança")
    client = MongoClient(mongo_url)
    return client, client[db_name][name]


def _login(base_url: str, email: str, password: str) -> requests.Session:
    session = requests.Session()
    session.headers.update({"Accept": "application/json", "Content-Type": "application/json"})
    response = session.post(
        f"{base_url}/api/auth/login",
        json={"email": email, "password": password},
        timeout=30,
    )
    assert response.status_code == 200, response.text
    token = response.json().get("access_token")
    assert token
    session.headers.update({"Authorization": f"Bearer {token}"})
    return session


def _ensure_test_walker() -> dict[str, Any]:
    mongo_client, users = _mongo_collection("users")
    try:
        existing = users.find_one({"email": TEST_WALKER_EMAIL}, {"_id": 0})
        now_iso = datetime.now(timezone.utc).isoformat()
        password_hash = bcrypt.hashpw(TEST_WALKER_PASSWORD.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

        if existing:
            users.update_one(
                {"id": existing["id"]},
                {
                    "$set": {
                        "full_name": TEST_WALKER_NAME,
                        "role": "passeador",
                        "isAdmin": False,
                        "isActive": True,
                        "password_hash": password_hash,
                        "availability_days": ["seg", "ter", "qua", "qui", "sex", "sab"],
                        "availability_start_time": "07:00",
                        "availability_end_time": "20:00",
                        "region": "Salvador/BA",
                        "updated_at": now_iso,
                    }
                },
            )
            return {"id": existing["id"], "full_name": TEST_WALKER_NAME}

        walker_id = str(uuid.uuid4())
        users.insert_one(
            {
                "id": walker_id,
                "full_name": TEST_WALKER_NAME,
                "email": TEST_WALKER_EMAIL,
                "password_hash": password_hash,
                "role": "passeador",
                "isAdmin": False,
                "permissions": {},
                "isActive": True,
                "created_by": "test_iteration28",
                "created_at": now_iso,
                "updated_at": now_iso,
                "last_active_at": now_iso,
                "availability_days": ["seg", "ter", "qua", "qui", "sex", "sab"],
                "availability_start_time": "07:00",
                "availability_end_time": "20:00",
                "region": "Salvador/BA",
                "quality_status": "ativo",
                "quality_status_reason": "Sem observações",
                "quality_monitoring": {
                    "active": False,
                    "severity": "padrao",
                    "target_walks": 7,
                    "completed_walks": 0,
                    "reset_count": 0,
                    "severe_delay_incidents": 0,
                    "course_completed": False,
                    "quiz_passed": False,
                    "quiz_attempts": 0,
                    "consecutive_quiz_failures": 0,
                    "review_recommended": False,
                },
                "quality_history": [],
            }
        )
        return {"id": walker_id, "full_name": TEST_WALKER_NAME}
    finally:
        mongo_client.close()


def _cleanup_test_walker_data(walker_user_id: str):
    mongo_client, db_users = _mongo_collection("users")
    db_walks = mongo_client[db_users.database.name]["walks"]
    try:
        db_walks.delete_many({"notes": {"$regex": r"^TEST_ITER28"}})
        db_walks.delete_many({"walker_user_id": walker_user_id, "pet_name": {"$regex": r"^TEST_ITER28"}})
        db_users.delete_one({"id": walker_user_id})
    finally:
        mongo_client.close()


def _walk_document(
    *,
    walker_user_id: str,
    walker_name: str,
    status: str,
    dt: datetime,
    rating: int | None = None,
    severe_delay: bool = False,
    client_confirmed: bool = False,
    walk_suffix: str = "",
) -> dict[str, Any]:
    now_iso = datetime.now(timezone.utc).isoformat()
    checkin_iso = None
    if severe_delay:
        checkin_iso = (dt + timedelta(minutes=20)).isoformat()

    return {
        "id": f"TEST_ITER28_{walk_suffix or uuid.uuid4().hex[:10]}",
        "pet_name": "TEST_ITER28_PET",
        "pet_ids": [],
        "shared_pet_names": [],
        "shared_client_names": [],
        "shared_owner_keys": [],
        "participant_user_ids": [],
        "client_user_id": None,
        "client_name": "TEST_ITER28_CLIENT",
        "walk_type": "Individual",
        "shared_context": None,
        "shared_approved": False,
        "shared_group": None,
        "walk_date": dt.strftime("%Y-%m-%d"),
        "walk_time": dt.strftime("%H:%M"),
        "duration_minutes": 30,
        "walker_id": f"partner-{walker_user_id}",
        "walker_user_id": walker_user_id,
        "walker_name": walker_name,
        "pickup_street": "Rua Teste",
        "pickup_number": "28",
        "pickup_neighborhood": "Pituba",
        "pickup_complement": "",
        "location_reference": "TEST_ITER28",
        "security_code": "1234",
        "did_pee": False,
        "did_poop": False,
        "rating": rating,
        "rating_comment": "",
        "summary_text": "",
        "pet_behavior_notes": "",
        "notes": "TEST_ITER28 auto",
        "motivoCancelamento": "",
        "tipoCancelamento": None,
        "penalidadePercentual": 0,
        "base_price": 45.0,
        "walker_payout": 33.75,
        "scheduled_start_at": dt.isoformat(),
        "walker_check_in_at": checkin_iso,
        "client_confirmed_at": dt.isoformat() if client_confirmed else None,
        "tolerance_expires_at": None,
        "attendance_message": "",
        "charged_amount": 45.0,
        "walker_payout_amount": 33.75,
        "platform_retained_amount": 11.25,
        "client_refund_amount": 0.0,
        "decision_resolved_at": None,
        "decision_source": "",
        "walker_penalty_registered": False,
        "status": status,
        "photo_url": None,
        "walk_datetime_iso": dt.isoformat(),
        "created_at": now_iso,
        "updated_at": now_iso,
    }


def _replace_walker_walks(walker_user_id: str, walks: list[dict[str, Any]]):
    mongo_client, db_walks = _mongo_collection("walks")
    try:
        db_walks.delete_many({"walker_user_id": walker_user_id, "notes": {"$regex": r"^TEST_ITER28"}})
        if walks:
            db_walks.insert_many(walks)
    finally:
        mongo_client.close()


def _get_walker_user(walker_user_id: str) -> dict[str, Any]:
    mongo_client, users = _mongo_collection("users")
    try:
        row = users.find_one({"id": walker_user_id}, {"_id": 0})
        assert row is not None
        return row
    finally:
        mongo_client.close()


def _set_monitoring(walker_user_id: str, monitoring: dict[str, Any], quality_status: str = "restrito"):
    mongo_client, users = _mongo_collection("users")
    try:
        users.update_one(
            {"id": walker_user_id},
            {
                "$set": {
                    "quality_status": quality_status,
                    "isActive": True,
                    "quality_monitoring": monitoring,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            },
        )
    finally:
        mongo_client.close()


def _trigger_recalculation(admin_session: requests.Session, base_url: str):
    response = admin_session.get(f"{base_url}/api/admin/walkers/performance", timeout=30)
    assert response.status_code == 200, response.text


@pytest.fixture(scope="module")
def quality_env(base_url: str):
    walker = _ensure_test_walker()
    admin_session = _login(base_url, ADMIN_CREDS["email"], ADMIN_CREDS["password"])
    client_session = _login(base_url, CLIENT_CREDS["email"], CLIENT_CREDS["password"])
    walker_session = _login(base_url, TEST_WALKER_EMAIL, TEST_WALKER_PASSWORD)

    yield {
        "admin": admin_session,
        "client": client_session,
        "walker": walker_session,
        "walker_user_id": walker["id"],
        "walker_name": walker["full_name"],
        "walker_partner_id": f"partner-{walker['id']}",
    }

    admin_session.close()
    client_session.close()
    walker_session.close()
    _cleanup_test_walker_data(walker["id"])


def test_quality_status_ignores_rating_rules_when_count_below_five(base_url: str, quality_env: dict[str, Any]):
    walker_user_id = quality_env["walker_user_id"]
    walker_name = quality_env["walker_name"]
    now = datetime.now(timezone.utc)
    walks = [
        _walk_document(
            walker_user_id=walker_user_id,
            walker_name=walker_name,
            status="Finalizado",
            dt=now - timedelta(days=idx + 1),
            rating=1,
            walk_suffix=f"LOW4_{idx}",
        )
        for idx in range(4)
    ]
    _replace_walker_walks(walker_user_id, walks)

    _trigger_recalculation(quality_env["admin"], base_url)
    walker_row = _get_walker_user(walker_user_id)
    assert walker_row.get("quality_status") in {"ativo", "em_observacao"}
    assert int((walker_row.get("quality_metrics") or {}).get("rating_count", 0)) == 4


def test_quality_status_applies_rating_rules_when_count_at_least_five(base_url: str, quality_env: dict[str, Any]):
    walker_user_id = quality_env["walker_user_id"]
    walker_name = quality_env["walker_name"]
    now = datetime.now(timezone.utc)
    # 4,4,4,3,3 -> média 3.6 (faixa restrita quando rating_count >= 5)
    ratings = [4, 4, 4, 3, 3]
    walks = []
    for idx, rating in enumerate(ratings):
        walks.append(
            _walk_document(
                walker_user_id=walker_user_id,
                walker_name=walker_name,
                status="Finalizado",
                dt=now - timedelta(days=idx + 1),
                rating=rating,
                walk_suffix=f"LOW5_{idx}",
            )
        )
    _replace_walker_walks(walker_user_id, walks)

    _trigger_recalculation(quality_env["admin"], base_url)
    walker_row = _get_walker_user(walker_user_id)
    assert walker_row.get("quality_status") == "restrito"


def test_premium_requires_zero_no_show_in_recent_ten(base_url: str, quality_env: dict[str, Any]):
    walker_user_id = quality_env["walker_user_id"]
    walker_name = quality_env["walker_name"]
    now = datetime.now(timezone.utc)
    premium_walks = [
        _walk_document(
            walker_user_id=walker_user_id,
            walker_name=walker_name,
            status="Finalizado",
            dt=now - timedelta(days=idx + 1),
            rating=5,
            walk_suffix=f"PREM_{idx}",
        )
        for idx in range(10)
    ]
    _replace_walker_walks(walker_user_id, premium_walks)
    _trigger_recalculation(quality_env["admin"], base_url)

    walker_row = _get_walker_user(walker_user_id)
    assert walker_row.get("quality_status") == "ativo_premium"

    no_show_recent = _walk_document(
        walker_user_id=walker_user_id,
        walker_name=walker_name,
        status="Não comparecimento do passeador",
        dt=now + timedelta(minutes=1),
        rating=None,
        walk_suffix="NOSHOW_RECENT",
    )
    _replace_walker_walks(walker_user_id, premium_walks + [no_show_recent])
    _trigger_recalculation(quality_env["admin"], base_url)
    walker_row = _get_walker_user(walker_user_id)
    assert walker_row.get("quality_status") == "restrito"


def test_recent_no_show_two_causes_suspension(base_url: str, quality_env: dict[str, Any]):
    walker_user_id = quality_env["walker_user_id"]
    walker_name = quality_env["walker_name"]
    now = datetime.now(timezone.utc)
    base = [
        _walk_document(
            walker_user_id=walker_user_id,
            walker_name=walker_name,
            status="Finalizado",
            dt=now - timedelta(days=idx + 3),
            rating=5,
            walk_suffix=f"GOOD_{idx}",
        )
        for idx in range(8)
    ]
    no_shows = [
        _walk_document(
            walker_user_id=walker_user_id,
            walker_name=walker_name,
            status="Não comparecimento do passeador",
            dt=now - timedelta(hours=idx),
            walk_suffix=f"NS2_{idx}",
        )
        for idx in range(2)
    ]
    _replace_walker_walks(walker_user_id, base + no_shows)
    _trigger_recalculation(quality_env["admin"], base_url)

    walker_row = _get_walker_user(walker_user_id)
    assert walker_row.get("quality_status") == "suspenso"


def test_severe_delay_recurrence_three_causes_suspension(base_url: str, quality_env: dict[str, Any]):
    walker_user_id = quality_env["walker_user_id"]
    walker_name = quality_env["walker_name"]
    now = datetime.now(timezone.utc)
    finished = [
        _walk_document(
            walker_user_id=walker_user_id,
            walker_name=walker_name,
            status="Finalizado",
            dt=now - timedelta(days=idx + 5),
            rating=5,
            walk_suffix=f"BASE_{idx}",
        )
        for idx in range(5)
    ]
    severe = [
        _walk_document(
            walker_user_id=walker_user_id,
            walker_name=walker_name,
            status="Finalizado",
            dt=now - timedelta(hours=idx + 1),
            rating=5,
            severe_delay=True,
            walk_suffix=f"SEV_{idx}",
        )
        for idx in range(3)
    ]
    _replace_walker_walks(walker_user_id, finished + severe)
    _trigger_recalculation(quality_env["admin"], base_url)

    walker_row = _get_walker_user(walker_user_id)
    assert walker_row.get("quality_status") == "suspenso"


def test_restricted_daily_limit_blocks_third_walk_creation(base_url: str, quality_env: dict[str, Any]):
    walker_user_id = quality_env["walker_user_id"]
    walker_name = quality_env["walker_name"]
    walker_partner_id = quality_env["walker_partner_id"]
    client = quality_env["client"]

    now = datetime.now(timezone.utc)
    target_date = None
    target_slots: list[str] = []
    for delta in range(1, 10):
        candidate_date = (now + timedelta(days=delta)).strftime("%Y-%m-%d")
        slots_resp = client.get(
            f"{base_url}/api/walkers/{walker_partner_id}/availability-slots",
            params={"date": candidate_date, "duration_minutes": 30},
            timeout=30,
        )
        if slots_resp.status_code == 200 and len(slots_resp.json().get("available_slots", [])) >= 2:
            target_date = candidate_date
            target_slots = slots_resp.json()["available_slots"]
            break

    if not target_date or len(target_slots) < 2:
        pytest.skip("Sem slots em dia útil para validar limite diário de restrito")

    first_dt = datetime.strptime(f"{target_date} {target_slots[0]}", "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    second_dt = datetime.strptime(f"{target_date} {target_slots[1]}", "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)

    first = _walk_document(
        walker_user_id=walker_user_id,
        walker_name=walker_name,
        status="Agendado",
        dt=first_dt,
        walk_suffix="LIM1",
    )
    second = _walk_document(
        walker_user_id=walker_user_id,
        walker_name=walker_name,
        status="Indo buscar o pet",
        dt=second_dt,
        walk_suffix="LIM2",
    )
    _replace_walker_walks(walker_user_id, [first, second])

    _set_monitoring(
        walker_user_id,
        {
            "active": True,
            "severity": "padrao",
            "target_walks": 7,
            "completed_walks": 0,
            "reset_count": 0,
            "severe_delay_incidents": 0,
            "course_completed": False,
            "quiz_passed": False,
            "quiz_attempts": 0,
            "consecutive_quiz_failures": 0,
            "review_recommended": False,
        },
        quality_status="restrito",
    )

    slot = target_slots[-1]

    pets_resp = client.get(f"{base_url}/api/pets", timeout=30)
    assert pets_resp.status_code == 200, pets_resp.text
    pets = pets_resp.json()
    if not pets:
        pytest.skip("Conta cliente sem pet para teste de criação de passeio")

    payload = {
        "pet_name": pets[0]["pet_name"],
        "pet_id": pets[0]["id"],
        "client_name": "TEST_ITER28_CLIENT",
        "walk_date": target_date,
        "walk_time": slot,
        "duration_minutes": 30,
        "walk_type": "Individual",
        "walker_id": walker_partner_id,
        "pickup_street": "Rua Teste 28",
        "pickup_number": "100",
        "pickup_neighborhood": "Pituba",
        "pickup_complement": "",
        "location_reference": "TEST_ITER28",
        "pet_behavior_notes": "",
        "notes": "TEST_ITER28 limit",
    }
    create_resp = client.post(f"{base_url}/api/walks", json=payload, timeout=30)
    assert create_resp.status_code == 400
    assert "limite diário" in create_resp.text


def test_structured_recovery_requires_checklist_and_allows_course_plus_quiz(base_url: str, quality_env: dict[str, Any]):
    walker_user_id = quality_env["walker_user_id"]
    walker_name = quality_env["walker_name"]
    walker_session = quality_env["walker"]
    now = datetime.now(timezone.utc)

    restricted_walks = [
        _walk_document(
            walker_user_id=walker_user_id,
            walker_name=walker_name,
            status="Finalizado",
            dt=now - timedelta(days=idx + 1),
            rating=4,
            walk_suffix=f"REC_{idx}",
        )
        for idx in range(5)
    ]
    restricted_walks.append(
        _walk_document(
            walker_user_id=walker_user_id,
            walker_name=walker_name,
            status="Não comparecimento do passeador",
            dt=now,
            walk_suffix="REC_NS",
        )
    )
    _replace_walker_walks(walker_user_id, restricted_walks)
    _trigger_recalculation(quality_env["admin"], base_url)

    fail_course = walker_session.post(
        f"{base_url}/api/walker/quality/course-complete",
        json={"checklist_confirmed": False},
        timeout=30,
    )
    assert fail_course.status_code == 400

    ok_course = walker_session.post(
        f"{base_url}/api/walker/quality/course-complete",
        json={"checklist_confirmed": True},
        timeout=30,
    )
    assert ok_course.status_code == 200, ok_course.text
    assert ok_course.json().get("course_completed") is True

    quiz = walker_session.post(
        f"{base_url}/api/walker/quality/quiz-submit",
        json={"answers": [1, 2, 3, 1, 2]},
        timeout=30,
    )
    assert quiz.status_code == 200, quiz.text
    assert quiz.json().get("approved") is True

    quality = walker_session.get(f"{base_url}/api/walker/quality", timeout=30)
    assert quality.status_code == 200, quality.text
    payload = quality.json()
    assert payload.get("recovery_required") is True
    assert payload.get("course_completed") is True
    assert payload.get("quiz_passed") is True
    assert "monitor_target_walks" in payload and "monitor_remaining_walks" in payload


def test_monitoring_resets_on_no_show_attendance_event(base_url: str, quality_env: dict[str, Any]):
    walker_user_id = quality_env["walker_user_id"]
    walker_name = quality_env["walker_name"]
    walker_session = quality_env["walker"]
    now = datetime.now(timezone.utc)

    history = [
        _walk_document(
            walker_user_id=walker_user_id,
            walker_name=walker_name,
            status="Não comparecimento do passeador",
            dt=now - timedelta(days=1),
            walk_suffix="RST_BASE_NS",
        )
    ]
    overdue = _walk_document(
        walker_user_id=walker_user_id,
        walker_name=walker_name,
        status="Agendado",
        dt=now - timedelta(hours=2),
        client_confirmed=True,
        walk_suffix="RST_OVERDUE",
    )
    _replace_walker_walks(walker_user_id, history + [overdue])
    _set_monitoring(
        walker_user_id,
        {
            "active": True,
            "severity": "padrao",
            "target_walks": 7,
            "completed_walks": 3,
            "reset_count": 0,
            "severe_delay_incidents": 0,
            "course_completed": True,
            "quiz_passed": True,
            "quiz_attempts": 1,
            "consecutive_quiz_failures": 0,
            "review_recommended": False,
        },
        quality_status="restrito",
    )

    tasks_resp = walker_session.get(f"{base_url}/api/walker/tasks", timeout=30)
    assert tasks_resp.status_code == 200, tasks_resp.text

    refreshed = _get_walker_user(walker_user_id)
    monitoring = refreshed.get("quality_monitoring") or {}
    assert int(monitoring.get("completed_walks", 0)) == 0
    assert int(monitoring.get("reset_count", 0)) >= 1


def test_monitoring_resets_after_third_severe_delay_checkin(base_url: str, quality_env: dict[str, Any]):
    walker_user_id = quality_env["walker_user_id"]
    walker_name = quality_env["walker_name"]
    walker_session = quality_env["walker"]
    now = datetime.now(timezone.utc)

    baseline = [
        _walk_document(
            walker_user_id=walker_user_id,
            walker_name=walker_name,
            status="Não comparecimento do passeador",
            dt=now - timedelta(days=2),
            walk_suffix="SEV_BASE_NS",
        )
    ]
    target_walk = _walk_document(
        walker_user_id=walker_user_id,
        walker_name=walker_name,
        status="Agendado",
        dt=now - timedelta(hours=1),
        walk_suffix="SEV_CHECKIN",
    )
    _replace_walker_walks(walker_user_id, baseline + [target_walk])
    _set_monitoring(
        walker_user_id,
        {
            "active": True,
            "severity": "padrao",
            "target_walks": 7,
            "completed_walks": 2,
            "reset_count": 0,
            "severe_delay_incidents": 2,
            "course_completed": True,
            "quiz_passed": True,
            "quiz_attempts": 1,
            "consecutive_quiz_failures": 0,
            "review_recommended": False,
        },
        quality_status="restrito",
    )

    checkin = walker_session.post(f"{base_url}/api/walks/{target_walk['id']}/check-in", timeout=30)
    assert checkin.status_code == 200, checkin.text

    refreshed = _get_walker_user(walker_user_id)
    monitoring = refreshed.get("quality_monitoring") or {}
    assert int(monitoring.get("completed_walks", 0)) == 0
    assert int(monitoring.get("severe_delay_incidents", 0)) == 0
    assert int(monitoring.get("reset_count", 0)) >= 1


def test_client_walkers_endpoint_hides_negative_internal_statuses(base_url: str, quality_env: dict[str, Any]):
    walker_user_id = quality_env["walker_user_id"]
    client = quality_env["client"]

    mongo_client, users = _mongo_collection("users")
    try:
        users.update_one({"id": walker_user_id}, {"$set": {"quality_status": "suspenso"}})
    finally:
        mongo_client.close()

    response = client.get(f"{base_url}/api/walkers", timeout=30)
    assert response.status_code == 200, response.text
    walkers = response.json()
    assert walkers and isinstance(walkers, list)
    assert all(item.get("quality_status") not in {"restrito", "suspenso"} for item in walkers)
