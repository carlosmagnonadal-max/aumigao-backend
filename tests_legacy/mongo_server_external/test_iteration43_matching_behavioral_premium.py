from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
import pytest
import requests
from dotenv import dotenv_values
from pymongo import MongoClient


# Módulo coberto: matching inteligente comportamental + premium boost controlado + anti-injustiça + pré-seleção

CLIENT_CREDS = {"email": "cliente@petpasso.com", "password": "Cliente@123"}


def _mongo_db():
    env = dotenv_values("/app/backend/.env")
    mongo_url = str(env.get("MONGO_URL") or "").strip().strip('"')
    db_name = str(env.get("DB_NAME") or "").strip().strip('"')
    if not mongo_url or not db_name:
        pytest.skip("MONGO_URL/DB_NAME indisponíveis")
    client = MongoClient(mongo_url)
    return client, client[db_name]


def _login(base_url: str, email: str, password: str) -> requests.Session:
    session = requests.Session()
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


def _create_test_walker(
    db,
    slug: str,
    *,
    region: str = "TEST_BAIRRO",
    quality_status: str = "ativo",
    disintermediation_flag: bool = False,
    behavior_risk_flag: bool = False,
    auto_preselection_suspended_minutes: int | None = None,
) -> dict[str, str]:
    email = f"test_iter43_{slug}@petpasso.com"
    full_name = f"TEST_ITER43 {slug}"
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()

    existing = db.users.find_one({"email": email}, {"_id": 0, "id": 1})
    walker_id = existing["id"] if existing else str(uuid.uuid4())

    suspended_until = None
    if auto_preselection_suspended_minutes and auto_preselection_suspended_minutes > 0:
        suspended_until = (now + timedelta(minutes=auto_preselection_suspended_minutes)).isoformat()

    payload = {
        "id": walker_id,
        "full_name": full_name,
        "email": email,
        "password_hash": bcrypt.hashpw("TestIter43@123".encode("utf-8"), bcrypt.gensalt()).decode("utf-8"),
        "role": "passeador",
        "isAdmin": False,
        "permissions": {},
        "isActive": True,
        "region": region,
        "quality_status": quality_status,
        "quality_status_reason": "TEST_ITER43 baseline",
        "availability_days": ["seg", "ter", "qua", "qui", "sex", "sab", "dom"],
        "availability_start_time": "00:00",
        "availability_end_time": "23:59",
        "availability_blocks": [],
        "unavailable_until": None,
        "match_penalty_points": 0.0,
        "match_penalty_until": None,
        "match_rejection_streak": 0,
        "match_cooldown_until": None,
        "flag_suspeita_desintermediacao": disintermediation_flag,
        "desintermediacao_flag_expires_at": (now + timedelta(days=3)).isoformat() if disintermediation_flag else None,
        "behavior_risk_flag_active": behavior_risk_flag,
        "behavior_risk_flag_until": (now + timedelta(days=3)).isoformat() if behavior_risk_flag else None,
        "auto_preselection_suspended_until": suspended_until,
        "created_at": now_iso,
        "updated_at": now_iso,
        "last_active_at": now_iso,
    }
    db.users.update_one({"id": walker_id}, {"$set": payload}, upsert=True)
    return {"id": walker_id, "name": full_name, "email": email, "partner_id": f"partner-{walker_id}"}


def _walk_doc(
    *,
    walker: dict[str, str],
    walk_dt: datetime,
    status: str = "Finalizado",
    rating: int | None = None,
    severe_delay: bool = False,
    neighborhood: str = "TEST_BAIRRO",
    note_prefix: str = "TEST_ITER43",
) -> dict[str, Any]:
    now_iso = datetime.now(timezone.utc).isoformat()
    check_in_at = (walk_dt + timedelta(minutes=22)).isoformat() if severe_delay else None
    return {
        "id": f"{note_prefix}_{uuid.uuid4().hex[:12]}",
        "pet_name": "TEST_ITER43_PET",
        "pet_ids": [],
        "shared_pet_names": [],
        "shared_client_names": [],
        "shared_owner_keys": [],
        "participant_user_ids": [],
        "client_user_id": None,
        "client_name": "TEST_ITER43_CLIENT",
        "walk_type": "Individual",
        "shared_context": None,
        "shared_approved": False,
        "shared_group": None,
        "walk_date": walk_dt.strftime("%Y-%m-%d"),
        "walk_time": walk_dt.strftime("%H:%M"),
        "duration_minutes": 30,
        "walker_id": walker["partner_id"],
        "walker_user_id": walker["id"],
        "walker_name": walker["name"],
        "pickup_street": "Rua Teste",
        "pickup_number": "43",
        "pickup_neighborhood": neighborhood,
        "pickup_complement": "",
        "location_reference": "TEST_ITER43",
        "security_code": "1234",
        "did_pee": False,
        "did_poop": False,
        "rating": rating,
        "rating_comment": "",
        "summary_text": "",
        "pet_behavior_notes": "",
        "notes": f"{note_prefix} auto",
        "motivoCancelamento": "",
        "tipoCancelamento": None,
        "penalidadePercentual": 0,
        "base_price": 45.0,
        "walker_payout": 33.75,
        "scheduled_start_at": walk_dt.isoformat(),
        "walker_check_in_at": check_in_at,
        "client_confirmed_at": None,
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
        "walk_datetime_iso": walk_dt.isoformat(),
        "created_at": now_iso,
        "updated_at": now_iso,
    }


def _seed_rating_history(db, walker: dict[str, str], ratings: list[int]):
    now = datetime.now(timezone.utc)
    docs = [
        _walk_doc(
            walker=walker,
            walk_dt=now - timedelta(days=idx + 1),
            status="Finalizado",
            rating=rating,
            note_prefix="TEST_ITER43",
        )
        for idx, rating in enumerate(ratings)
    ]
    if docs:
        db.walks.insert_many(docs)


def _matching_payload(*, walk_date: str, walk_time: str, neighborhood: str = "TEST_BAIRRO") -> dict[str, Any]:
    return {
        "pet_name": "TEST_ITER43_MATCH_PET",
        "client_name": "TEST_ITER43_CLIENT",
        "walk_date": walk_date,
        "walk_time": walk_time,
        "duration_minutes": 30,
        "walk_type": "Individual",
        "modo_inicio_passeio": "endereco_tutor",
        "pickup_street": "Rua Teste Iter43",
        "pickup_number": "100",
        "pickup_neighborhood": neighborhood,
        "pickup_complement": "",
        "location_reference": "TEST_ITER43",
        "pet_behavior_notes": "",
        "notes": "TEST_ITER43 matching",
    }


@pytest.fixture(scope="function")
def matching_env(base_url: str):
    mongo_client, db = _mongo_db()
    created_user_ids: list[str] = []
    try:
        db.walks.delete_many({"notes": {"$regex": r"^TEST_ITER43"}})
        db.walks.delete_many({"pet_name": {"$regex": r"^TEST_ITER43"}})
        db.users.delete_many({"email": {"$regex": r"^test_iter43_.*@petpasso\.com$"}})
        db.matching_requests.delete_many({"client_name": {"$regex": r"^TEST_ITER43"}})
        db.walker_requests.delete_many({"client_name": {"$regex": r"^TEST_ITER43"}})

        yield {"db": db, "base_url": base_url, "created_user_ids": created_user_ids}
    finally:
        db.walks.delete_many({"notes": {"$regex": r"^TEST_ITER43"}})
        db.walks.delete_many({"pet_name": {"$regex": r"^TEST_ITER43"}})
        db.walker_requests.delete_many({"client_name": {"$regex": r"^TEST_ITER43"}})
        db.matching_requests.delete_many({"client_name": {"$regex": r"^TEST_ITER43"}})
        if created_user_ids:
            db.users.delete_many({"id": {"$in": created_user_ids}})
        db.users.delete_many({"email": {"$regex": r"^test_iter43_.*@petpasso\.com$"}})
        mongo_client.close()


def _new_walker(env: dict[str, Any], slug: str, **kwargs) -> dict[str, str]:
    walker = _create_test_walker(env["db"], slug, **kwargs)
    env["created_user_ids"].append(walker["id"])
    return walker


def test_behavioral_score_formula_components_exposed(matching_env):
    db = matching_env["db"]
    base_url = matching_env["base_url"]
    client = _login(base_url, CLIENT_CREDS["email"], CLIENT_CREDS["password"])
    walk_date = (datetime.now(timezone.utc) + timedelta(days=2)).strftime("%Y-%m-%d")

    walker = _new_walker(matching_env, "formula", quality_status="ativo")
    _seed_rating_history(db, walker, [5, 5, 5, 4, 5, 5])

    resp = client.post(
        f"{base_url}/api/walks/matching-request",
        json=_matching_payload(walk_date=walk_date, walk_time="13:15"),
        timeout=30,
    )
    assert resp.status_code == 201, resp.text
    row = db.matching_requests.find_one({"id": resp.json()["id"]}, {"_id": 0})
    assert row and row.get("candidates")
    candidate = row["candidates"][0]

    expected_behavioral = (
        float(candidate.get("score_base_component", 0.0)) * 0.40
        + float(candidate.get("score_distancia_component", 0.0)) * 0.25
        + float(candidate.get("score_confiabilidade_component", 0.0)) * 0.20
        + float(candidate.get("score_disponibilidade_component", 0.0)) * 0.15
        + float(candidate.get("premium_boost_points", 0.0))
    )

    assert float(candidate.get("behavioral_match_score", 0.0)) == pytest.approx(expected_behavioral, abs=0.05)
    assert "match_score" in candidate  # compatibilidade legado
    client.close()


def test_premium_boost_only_when_base_component_at_least_70(matching_env):
    db = matching_env["db"]
    base_url = matching_env["base_url"]
    client = _login(base_url, CLIENT_CREDS["email"], CLIENT_CREDS["password"])
    walk_date = (datetime.now(timezone.utc) + timedelta(days=3)).strftime("%Y-%m-%d")

    premium_low_base = _new_walker(matching_env, "premium_low_base", quality_status="ativo_premium")
    _seed_rating_history(db, premium_low_base, [3, 3, 3, 3, 3, 3])

    resp = client.post(
        f"{base_url}/api/walks/matching-request",
        json=_matching_payload(walk_date=walk_date, walk_time="13:15"),
        timeout=30,
    )
    assert resp.status_code == 201, resp.text
    row = db.matching_requests.find_one({"id": resp.json()["id"]}, {"_id": 0})
    assert row and row.get("candidates")
    candidate = next((c for c in row["candidates"] if c.get("walker_user_id") == premium_low_base["id"]), None)
    assert candidate is not None
    assert float(candidate.get("score_base_component", 0.0)) < 70.0
    assert float(candidate.get("premium_boost_points", -1.0)) == pytest.approx(0.0)
    client.close()


def test_premium_boost_reduced_to_five_with_recent_negative_behavior(matching_env):
    db = matching_env["db"]
    base_url = matching_env["base_url"]
    client = _login(base_url, CLIENT_CREDS["email"], CLIENT_CREDS["password"])
    walk_date = (datetime.now(timezone.utc) + timedelta(days=4)).strftime("%Y-%m-%d")

    premium_negative = _new_walker(
        matching_env,
        "premium_negative",
        quality_status="ativo_premium",
        disintermediation_flag=True,
    )
    _seed_rating_history(db, premium_negative, [5, 5, 5, 5, 5, 5, 5])

    resp = client.post(
        f"{base_url}/api/walks/matching-request",
        json=_matching_payload(walk_date=walk_date, walk_time="13:15"),
        timeout=30,
    )
    assert resp.status_code == 201, resp.text
    row = db.matching_requests.find_one({"id": resp.json()["id"]}, {"_id": 0})
    assert row and row.get("candidates")
    candidate = next((c for c in row["candidates"] if c.get("walker_user_id") == premium_negative["id"]), None)
    assert candidate is not None
    assert float(candidate.get("score_base_component", 0.0)) >= 70.0
    assert float(candidate.get("premium_boost_points", 0.0)) == pytest.approx(5.0)
    client.close()


def test_premium_boost_twelve_in_regional_high_demand_threshold(matching_env):
    db = matching_env["db"]
    base_url = matching_env["base_url"]
    client = _login(base_url, CLIENT_CREDS["email"], CLIENT_CREDS["password"])
    target_dt = datetime.now(timezone.utc) + timedelta(days=5)
    walk_date = target_dt.strftime("%Y-%m-%d")
    neighborhood = "TEST_BAIRRO"

    premium_high_demand = _new_walker(matching_env, "premium_high_demand", quality_status="ativo_premium", region=neighborhood)
    _seed_rating_history(db, premium_high_demand, [5, 5, 5, 5, 4, 5])

    feeder = _new_walker(matching_env, "demand_feeder", quality_status="ativo", region=neighborhood)
    for i in range(6):
        feeder_walk = _walk_doc(
            walker=feeder,
            walk_dt=target_dt.replace(hour=13, minute=10 + i),
            status="Agendado",
            rating=None,
            neighborhood=neighborhood,
            note_prefix="TEST_ITER43",
        )
        db.walks.insert_one(feeder_walk)

    resp = client.post(
        f"{base_url}/api/walks/matching-request",
        json=_matching_payload(walk_date=walk_date, walk_time="13:15", neighborhood=neighborhood),
        timeout=30,
    )
    assert resp.status_code == 201, resp.text
    row = db.matching_requests.find_one({"id": resp.json()["id"]}, {"_id": 0})
    assert row and row.get("candidates")
    candidate = next((c for c in row["candidates"] if c.get("walker_user_id") == premium_high_demand["id"]), None)
    assert candidate is not None
    assert float(candidate.get("premium_boost_points", 0.0)) == pytest.approx(12.0)
    client.close()


def test_anti_unfairness_distance_rule_penalizes_far_premium(matching_env):
    base_url = matching_env["base_url"]
    client = _login(base_url, CLIENT_CREDS["email"], CLIENT_CREDS["password"])
    walk_date = (datetime.now(timezone.utc) + timedelta(days=6)).strftime("%Y-%m-%d")

    close_non_premium = _new_walker(matching_env, "close_non_premium", quality_status="ativo", region="TEST_BAIRRO")
    far_premium = _new_walker(matching_env, "far_premium", quality_status="ativo_premium", region="BAIRRO_DISTANTE")

    _seed_rating_history(matching_env["db"], close_non_premium, [5, 5, 4, 5, 5, 5])
    _seed_rating_history(matching_env["db"], far_premium, [5, 5, 5, 5, 5, 5])

    response = client.get(
        f"{base_url}/api/walkers",
        params={"date": walk_date, "duration_minutes": 30, "preferred_time": "13:15", "neighborhood": "TEST_BAIRRO"},
        timeout=30,
    )
    assert response.status_code == 200, response.text
    walkers = response.json()
    premium_row = next((w for w in walkers if str(w.get("id")) == f"partner-{far_premium['id']}"), None)
    assert premium_row is not None
    assert float(premium_row.get("score_distancia_component", 100.0)) <= 44.1
    client.close()


def test_behavioral_score_below_55_never_enters_candidates(matching_env):
    db = matching_env["db"]
    base_url = matching_env["base_url"]
    client = _login(base_url, CLIENT_CREDS["email"], CLIENT_CREDS["password"])
    walk_date = (datetime.now(timezone.utc) + timedelta(days=7)).strftime("%Y-%m-%d")

    good = _new_walker(matching_env, "good", quality_status="ativo", region="TEST_BAIRRO")
    low = _new_walker(matching_env, "low_behavior", quality_status="ativo", region="TEST_BAIRRO")

    _seed_rating_history(db, good, [5, 5, 5, 4, 5, 5])
    _seed_rating_history(db, low, [1, 1, 1, 1, 1, 1])

    now = datetime.now(timezone.utc)
    db.walks.insert_one(_walk_doc(walker=low, walk_dt=now - timedelta(hours=2), status="Não comparecimento do passeador", rating=None))
    db.walks.insert_one(_walk_doc(walker=low, walk_dt=now - timedelta(hours=4), status="Não comparecimento do passeador", rating=None))

    resp = client.post(
        f"{base_url}/api/walks/matching-request",
        json=_matching_payload(walk_date=walk_date, walk_time="13:15"),
        timeout=30,
    )
    assert resp.status_code == 201, resp.text
    row = db.matching_requests.find_one({"id": resp.json()["id"]}, {"_id": 0})
    assert row and row.get("candidates")
    candidate_ids = {str(item.get("walker_user_id") or "") for item in row["candidates"]}
    assert good["id"] in candidate_ids
    assert low["id"] not in candidate_ids
    client.close()


def test_top1_and_top3_highlights_still_work(matching_env):
    db = matching_env["db"]
    base_url = matching_env["base_url"]
    client = _login(base_url, CLIENT_CREDS["email"], CLIENT_CREDS["password"])
    walk_date = (datetime.now(timezone.utc) + timedelta(days=8)).strftime("%Y-%m-%d")

    for idx in range(4):
        walker = _new_walker(matching_env, f"highlight_{idx}", quality_status="ativo", region="TEST_BAIRRO")
        _seed_rating_history(db, walker, [5, 5, 4, 5, 5, 5])

    response = client.get(
        f"{base_url}/api/walkers",
        params={"date": walk_date, "duration_minutes": 30, "preferred_time": "13:15", "neighborhood": "TEST_BAIRRO"},
        timeout=30,
    )
    assert response.status_code == 200, response.text
    walkers = response.json()
    assert len(walkers) >= 3

    top = [w for w in walkers if bool(w.get("is_top_match"))]
    assert len(top) == 1
    assert walkers[0].get("selection_reason") == "Destaques da semana"
    assert walkers[1].get("selection_reason") == "Passeadores em alta"
    assert walkers[2].get("selection_reason") == "Recomendados na sua região"
    client.close()


def test_risk_flags_and_auto_preselection_suspension_block_candidates(matching_env):
    db = matching_env["db"]
    base_url = matching_env["base_url"]
    client = _login(base_url, CLIENT_CREDS["email"], CLIENT_CREDS["password"])
    walk_date = (datetime.now(timezone.utc) + timedelta(days=9)).strftime("%Y-%m-%d")

    eligible = _new_walker(matching_env, "eligible", quality_status="ativo", region="TEST_BAIRRO")
    risk_flagged = _new_walker(matching_env, "risk_flagged", quality_status="ativo", behavior_risk_flag=True, region="TEST_BAIRRO")
    suspended = _new_walker(
        matching_env,
        "suspended_preselection",
        quality_status="ativo",
        auto_preselection_suspended_minutes=120,
        region="TEST_BAIRRO",
    )

    _seed_rating_history(db, eligible, [5, 5, 5, 4, 5, 5])
    _seed_rating_history(db, risk_flagged, [5, 5, 5, 4, 5, 5])
    _seed_rating_history(db, suspended, [5, 5, 5, 4, 5, 5])

    resp = client.post(
        f"{base_url}/api/walks/matching-request",
        json=_matching_payload(walk_date=walk_date, walk_time="13:15"),
        timeout=30,
    )
    assert resp.status_code == 201, resp.text
    row = db.matching_requests.find_one({"id": resp.json()["id"]}, {"_id": 0})
    assert row and row.get("candidates")

    candidate_ids = {str(item.get("walker_user_id") or "") for item in row["candidates"]}
    by_id = {str(item.get("walker_user_id") or ""): item for item in row["candidates"]}
    assert eligible["id"] in candidate_ids
    assert suspended["id"] not in candidate_ids
    assert risk_flagged["id"] in candidate_ids
    assert float(by_id[eligible["id"]].get("behavioral_match_score", 0.0)) > float(
        by_id[risk_flagged["id"]].get("behavioral_match_score", 0.0)
    )
    assert int(by_id[risk_flagged["id"]].get("rank_position", 0) or 0) > int(
        by_id[eligible["id"]].get("rank_position", 0) or 0
    )
    client.close()
