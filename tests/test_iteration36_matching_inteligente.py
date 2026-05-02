from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
import pytest
import requests
from dotenv import dotenv_values
from pymongo import MongoClient


# Módulo coberto: matching inteligente (elegibilidade, ranking, ondas, fallback, lock, penalidade, cooldown, anti-spam)

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


def _create_test_walker(db, slug: str, *, region: str = "TEST_BAIRRO") -> dict[str, str]:
    email = f"test_iter36_{slug}@petpasso.com"
    full_name = f"TEST_ITER36 {slug}"
    now_iso = datetime.now(timezone.utc).isoformat()

    existing = db.users.find_one({"email": email}, {"_id": 0, "id": 1})
    walker_id = existing["id"] if existing else str(uuid.uuid4())

    payload = {
        "id": walker_id,
        "full_name": full_name,
        "email": email,
        "password_hash": bcrypt.hashpw("TestIter36@123".encode("utf-8"), bcrypt.gensalt()).decode("utf-8"),
        "role": "passeador",
        "isAdmin": False,
        "permissions": {},
        "isActive": True,
        "region": region,
        "quality_status": "ativo",
        "quality_status_reason": "TEST_ITER36 baseline",
        "availability_days": ["seg", "ter", "qua", "qui", "sex", "sab", "dom"],
        "availability_start_time": "00:00",
        "availability_end_time": "23:59",
        "availability_blocks": [],
        "unavailable_until": None,
        "match_penalty_points": 0.0,
        "match_penalty_until": None,
        "match_rejection_streak": 0,
        "match_cooldown_until": None,
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
    note_prefix: str = "TEST_ITER36",
) -> dict[str, Any]:
    now_iso = datetime.now(timezone.utc).isoformat()
    check_in_at = (walk_dt + timedelta(minutes=20)).isoformat() if severe_delay else None
    return {
        "id": f"{note_prefix}_{uuid.uuid4().hex[:12]}",
        "pet_name": "TEST_ITER36_PET",
        "pet_ids": [],
        "shared_pet_names": [],
        "shared_client_names": [],
        "shared_owner_keys": [],
        "participant_user_ids": [],
        "client_user_id": None,
        "client_name": "TEST_ITER36_CLIENT",
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
        "pickup_number": "36",
        "pickup_neighborhood": "TEST_BAIRRO",
        "pickup_complement": "",
        "location_reference": "TEST_ITER36",
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


def _seed_good_history(db, walker: dict[str, str], *, count: int = 6):
    now = datetime.now(timezone.utc)
    docs = [_walk_doc(walker=walker, walk_dt=now - timedelta(days=i + 1), status="Finalizado", rating=5) for i in range(count)]
    db.walks.insert_many(docs)


def _matching_payload(*, walk_date: str, walk_time: str) -> dict[str, Any]:
    return {
        "pet_name": "TEST_ITER36_MATCH_PET",
        "client_name": "TEST_ITER36_CLIENT",
        "walk_date": walk_date,
        "walk_time": walk_time,
        "duration_minutes": 30,
        "walk_type": "Individual",
        "modo_inicio_passeio": "endereco_tutor",
        "pickup_street": "Rua Teste Iter36",
        "pickup_number": "100",
        "pickup_neighborhood": "TEST_BAIRRO",
        "pickup_complement": "",
        "location_reference": "TEST_ITER36",
        "pet_behavior_notes": "",
        "notes": "TEST_ITER36 matching",
    }


@pytest.fixture(scope="function")
def matching_env(base_url: str):
    mongo_client, db = _mongo_db()
    created_user_ids: list[str] = []
    try:
        db.walks.delete_many({"notes": {"$regex": r"^TEST_ITER36"}})
        db.walks.delete_many({"pet_name": {"$regex": r"^TEST_ITER36"}})
        db.users.delete_many({"email": {"$regex": r"^test_iter36_.*@petpasso\.com$"}})
        db.matching_requests.delete_many({"client_name": {"$regex": r"^TEST_ITER36"}})
        db.walker_requests.delete_many({"client_name": {"$regex": r"^TEST_ITER36"}})

        yield {"db": db, "base_url": base_url, "created_user_ids": created_user_ids}
    finally:
        db.walks.delete_many({"notes": {"$regex": r"^TEST_ITER36"}})
        db.walks.delete_many({"pet_name": {"$regex": r"^TEST_ITER36"}})
        db.walker_requests.delete_many({"client_name": {"$regex": r"^TEST_ITER36"}})
        db.matching_requests.delete_many({"client_name": {"$regex": r"^TEST_ITER36"}})
        if created_user_ids:
            db.users.delete_many({"id": {"$in": created_user_ids}})
        db.users.delete_many({"email": {"$regex": r"^test_iter36_.*@petpasso\.com$"}})
        mongo_client.close()


def _new_walker(env: dict[str, Any], slug: str, region: str = "TEST_BAIRRO") -> dict[str, str]:
    walker = _create_test_walker(env["db"], slug, region=region)
    env["created_user_ids"].append(walker["id"])
    return walker


def test_matching_request_creates_and_dispatches_initial_wave(matching_env):
    db = matching_env["db"]
    base_url = matching_env["base_url"]
    client = _login(base_url, CLIENT_CREDS["email"], CLIENT_CREDS["password"])
    now = datetime.now(timezone.utc)
    walk_date = (now + timedelta(days=2)).strftime("%Y-%m-%d")

    walkers = [_new_walker(matching_env, f"wave1_{idx}") for idx in range(3)]
    for walker in walkers:
        _seed_good_history(db, walker)

    resp = client.post(f"{base_url}/api/walks/matching-request", json=_matching_payload(walk_date=walk_date, walk_time="23:15"), timeout=30)
    assert resp.status_code == 201, resp.text
    payload = resp.json()
    assert payload["status"] == "searching"
    assert payload["current_wave"] == 1
    assert "Buscando" in payload["client_message"]

    pending = list(db.walker_requests.find({"matching_request_id": payload["id"], "status": "pending"}, {"_id": 0}))
    assert len(pending) == 1
    assert int(pending[0].get("wave", 0)) == 1
    client.close()


def test_matching_status_advances_wave_after_timeout(matching_env):
    db = matching_env["db"]
    base_url = matching_env["base_url"]
    client = _login(base_url, CLIENT_CREDS["email"], CLIENT_CREDS["password"])
    walk_date = (datetime.now(timezone.utc) + timedelta(days=3)).strftime("%Y-%m-%d")

    walkers = [_new_walker(matching_env, f"wave2_{idx}") for idx in range(4)]
    for walker in walkers:
        _seed_good_history(db, walker)

    created = client.post(
        f"{base_url}/api/walks/matching-request",
        json=_matching_payload(walk_date=walk_date, walk_time="23:15"),
        timeout=30,
    )
    assert created.status_code == 201, created.text
    matching_id = created.json()["id"]

    past_iso = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    db.walker_requests.update_many(
        {"matching_request_id": matching_id, "status": "pending"},
        {"$set": {"respond_until": past_iso, "updated_at": past_iso}},
    )
    db.matching_requests.update_one({"id": matching_id}, {"$set": {"wave_expires_at": past_iso}})

    status = client.get(f"{base_url}/api/walks/matching-request/{matching_id}", timeout=30)
    assert status.status_code == 200, status.text
    body = status.json()
    assert body["current_wave"] == 2
    assert body["ignored_count"] >= 1

    wave2_pending = list(
        db.walker_requests.find(
            {"matching_request_id": matching_id, "status": "pending", "wave": 2},
            {"_id": 0},
        )
    )
    assert len(wave2_pending) in {1, 2}
    client.close()


def test_matching_eligibility_excludes_blocked_cases(matching_env):
    db = matching_env["db"]
    base_url = matching_env["base_url"]
    client = _login(base_url, CLIENT_CREDS["email"], CLIENT_CREDS["password"])
    now = datetime.now(timezone.utc)
    walk_date = (now + timedelta(days=4)).strftime("%Y-%m-%d")

    eligible = _new_walker(matching_env, "eligible")
    low_score = _new_walker(matching_env, "low_score")
    no_show = _new_walker(matching_env, "noshow")
    active_delay = _new_walker(matching_env, "active_delay")
    active_walk = _new_walker(matching_env, "active_walk")

    _seed_good_history(db, eligible)

    low_docs = [_walk_doc(walker=low_score, walk_dt=now - timedelta(days=i + 1), status="Finalizado", rating=1, severe_delay=True) for i in range(6)]
    low_docs.extend(
        [_walk_doc(walker=low_score, walk_dt=now - timedelta(days=10 + i), status="Não comparecimento do passeador", rating=None) for i in range(3)]
    )
    db.walks.insert_many(low_docs)

    _seed_good_history(db, no_show)
    db.walks.insert_many(
        [
            _walk_doc(walker=no_show, walk_dt=now - timedelta(hours=2), status="Não comparecimento do passeador", rating=None),
            _walk_doc(walker=no_show, walk_dt=now - timedelta(hours=6), status="Não comparecimento do passeador", rating=None),
        ]
    )

    _seed_good_history(db, active_delay)
    delay_walk = _walk_doc(walker=active_delay, walk_dt=now, status="Passeando agora", rating=None)
    delay_walk["occurrence_status"] = "atraso_grave"
    delay_walk["occurrence_resolved"] = False
    db.walks.insert_one(delay_walk)

    _seed_good_history(db, active_walk)
    db.walks.insert_one(_walk_doc(walker=active_walk, walk_dt=now + timedelta(hours=2), status="Agendado", rating=None))

    create = client.post(
        f"{base_url}/api/walks/matching-request",
        json=_matching_payload(walk_date=walk_date, walk_time="23:15"),
        timeout=30,
    )
    assert create.status_code == 201, create.text
    matching_id = create.json()["id"]

    matching = db.matching_requests.find_one({"id": matching_id}, {"_id": 0})
    assert matching is not None
    candidates = matching.get("candidates", [])
    candidate_user_ids = {item.get("walker_user_id") for item in candidates}

    assert eligible["id"] in candidate_user_ids
    assert low_score["id"] not in candidate_user_ids
    assert no_show["id"] not in candidate_user_ids
    assert active_delay["id"] not in candidate_user_ids
    assert active_walk["id"] not in candidate_user_ids
    client.close()


def test_match_score_formula_and_ranking_order(matching_env):
    db = matching_env["db"]
    base_url = matching_env["base_url"]
    client = _login(base_url, CLIENT_CREDS["email"], CLIENT_CREDS["password"])
    walk_date = (datetime.now(timezone.utc) + timedelta(days=5)).strftime("%Y-%m-%d")

    walkers = [_new_walker(matching_env, f"formula_{idx}") for idx in range(4)]
    for idx, walker in enumerate(walkers):
        _seed_good_history(db, walker, count=6 - min(idx, 2))

    response = client.post(
        f"{base_url}/api/walks/matching-request",
        json=_matching_payload(walk_date=walk_date, walk_time="23:15"),
        timeout=30,
    )
    assert response.status_code == 201, response.text
    matching = db.matching_requests.find_one({"id": response.json()["id"]}, {"_id": 0})
    assert matching
    candidates = matching.get("candidates", [])
    assert len(candidates) >= 2

    for candidate in candidates:
        expected = (
            (float(candidate.get("score_final", 0)) * 0.50)
            + (float(candidate.get("proximity_score", 0)) * 0.25)
            + (float(candidate.get("availability_score", 0)) * 0.15)
            + (float(candidate.get("load_balance_score", 0)) * 0.10)
        )
        assert float(candidate.get("match_score", 0)) == pytest.approx(expected, abs=0.05)

    tuples = [
        (
            float(item.get("match_score", 0)),
            float(item.get("score_final", 0)),
            float(item.get("proximity_score", 0)),
        )
        for item in candidates
    ]
    assert tuples == sorted(tuples, key=lambda t: (-t[0], -t[1], -t[2]))
    client.close()


def test_fallback_low_supply_sets_min_score_50_and_broader_send(matching_env):
    db = matching_env["db"]
    base_url = matching_env["base_url"]
    client = _login(base_url, CLIENT_CREDS["email"], CLIENT_CREDS["password"])
    walk_date = (datetime.now(timezone.utc) + timedelta(days=6)).strftime("%Y-%m-%d")

    w1 = _new_walker(matching_env, "fallback_1")
    w2 = _new_walker(matching_env, "fallback_2")
    _seed_good_history(db, w1)
    _seed_good_history(db, w2)

    response = client.post(
        f"{base_url}/api/walks/matching-request",
        json=_matching_payload(walk_date=walk_date, walk_time="23:15"),
        timeout=30,
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["fallback_mode"] is True
    assert float(body["min_score_threshold"]) == pytest.approx(50.0)

    pending = list(db.walker_requests.find({"matching_request_id": body["id"], "status": "pending"}, {"_id": 0}))
    assert len(pending) == 2
    assert all(int(item.get("wave", 0)) == 1 for item in pending)
    client.close()


def test_accept_lock_blocks_duplicate_and_cancels_pending(matching_env):
    db = matching_env["db"]
    base_url = matching_env["base_url"]
    client = _login(base_url, CLIENT_CREDS["email"], CLIENT_CREDS["password"])
    walk_date = (datetime.now(timezone.utc) + timedelta(days=7)).strftime("%Y-%m-%d")

    wa = _new_walker(matching_env, "accept_a")
    wb = _new_walker(matching_env, "accept_b")
    _seed_good_history(db, wa)
    _seed_good_history(db, wb)

    create = client.post(
        f"{base_url}/api/walks/matching-request",
        json=_matching_payload(walk_date=walk_date, walk_time="23:15"),
        timeout=30,
    )
    assert create.status_code == 201, create.text
    matching_id = create.json()["id"]

    requests_rows = list(db.walker_requests.find({"matching_request_id": matching_id, "status": "pending"}, {"_id": 0}))
    assert len(requests_rows) == 2
    req_a = next(row for row in requests_rows if row.get("target_walker_user_id") == wa["id"])
    req_b = next(row for row in requests_rows if row.get("target_walker_user_id") == wb["id"])

    walker_a_session = _login(base_url, wa["email"], "TestIter36@123")
    accepted = walker_a_session.post(
        f"{base_url}/api/walker/requests/{req_a['id']}/decision",
        json={"decision": "accept"},
        timeout=30,
    )
    assert accepted.status_code == 200, accepted.text
    walker_a_session.close()

    walker_b_session = _login(base_url, wb["email"], "TestIter36@123")
    duplicate = walker_b_session.post(
        f"{base_url}/api/walker/requests/{req_b['id']}/decision",
        json={"decision": "accept"},
        timeout=30,
    )
    assert duplicate.status_code in {404, 409}
    walker_b_session.close()

    match = db.matching_requests.find_one({"id": matching_id}, {"_id": 0})
    assert match and match.get("status") == "matched"
    assert match.get("selected_walker_user_id") == wa["id"]

    req_b_db = db.walker_requests.find_one({"id": req_b["id"]}, {"_id": 0})
    assert req_b_db and req_b_db.get("status") == "canceled"
    client.close()


def test_reject_ignore_penalty_and_cooldown_after_three_consecutive(matching_env):
    db = matching_env["db"]
    base_url = matching_env["base_url"]
    walker = _new_walker(matching_env, "penalty")
    walker_session = _login(base_url, walker["email"], "TestIter36@123")
    now = datetime.now(timezone.utc)

    created_request_ids = []
    for idx in range(3):
        matching_id = f"iter36-penalty-match-{uuid.uuid4().hex[:8]}"
        db.matching_requests.insert_one(
            {
                "id": matching_id,
                "status": "searching",
                "requested_by_user_id": "test-client",
                "client_name": "TEST_ITER36_CLIENT",
                "walk_date": (now + timedelta(days=8)).strftime("%Y-%m-%d"),
                "walk_time": "05:00",
                "duration_minutes": 30,
                "walk_type": "Individual",
                "created_at": now.isoformat(),
                "updated_at": now.isoformat(),
            }
        )
        req_id = f"iter36-penalty-req-{idx}-{uuid.uuid4().hex[:6]}"
        created_request_ids.append(req_id)
        db.walker_requests.insert_one(
            {
                "id": req_id,
                "matching_request_id": matching_id,
                "wave": 1,
                "rank_position": idx + 1,
                "match_score": 70,
                "score_final": 70,
                "proximity_score": 80,
                "availability_score": 80,
                "load_balance_score": 100,
                "pet_name": "TEST_ITER36_PET",
                "client_name": "TEST_ITER36_CLIENT",
                "neighborhood": "TEST_BAIRRO",
                "approx_location": "TEST_ITER36",
                "walk_date": (now + timedelta(days=8)).strftime("%Y-%m-%d"),
                "walk_time": "05:00",
                "duration_minutes": 30,
                "walk_type": "Individual",
                "region": "TEST_BAIRRO",
                "status": "pending",
                "target_walker_user_id": walker["id"],
                "respond_until": (now + timedelta(minutes=5)).isoformat(),
                "created_at": now.isoformat(),
                "updated_at": now.isoformat(),
            }
        )

    for req_id in created_request_ids:
        decision = walker_session.post(
            f"{base_url}/api/walker/requests/{req_id}/decision",
            json={"decision": "reject"},
            timeout=30,
        )
        assert decision.status_code == 200, decision.text

    walker_after_rejects = db.users.find_one({"id": walker["id"]}, {"_id": 0})
    assert walker_after_rejects is not None
    assert float(walker_after_rejects.get("match_penalty_points", 0.0)) >= 2.0
    cooldown_until = walker_after_rejects.get("match_cooldown_until")
    assert isinstance(cooldown_until, str) and cooldown_until

    late_match_id = f"iter36-ignore-match-{uuid.uuid4().hex[:8]}"
    late_req_id = f"iter36-ignore-req-{uuid.uuid4().hex[:8]}"
    db.matching_requests.insert_one(
        {
            "id": late_match_id,
            "status": "searching",
            "requested_by_user_id": "test-client",
            "client_name": "TEST_ITER36_CLIENT",
            "walk_date": (now + timedelta(days=9)).strftime("%Y-%m-%d"),
            "walk_time": "05:15",
            "duration_minutes": 30,
            "walk_type": "Individual",
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
        }
    )
    db.walker_requests.insert_one(
        {
            "id": late_req_id,
            "matching_request_id": late_match_id,
            "wave": 1,
            "rank_position": 1,
            "match_score": 70,
            "score_final": 70,
            "proximity_score": 80,
            "availability_score": 80,
            "load_balance_score": 100,
            "pet_name": "TEST_ITER36_PET",
            "client_name": "TEST_ITER36_CLIENT",
            "neighborhood": "TEST_BAIRRO",
            "approx_location": "TEST_ITER36",
            "walk_date": (now + timedelta(days=9)).strftime("%Y-%m-%d"),
            "walk_time": "05:15",
            "duration_minutes": 30,
            "walk_type": "Individual",
            "region": "TEST_BAIRRO",
            "status": "pending",
            "target_walker_user_id": walker["id"],
            "respond_until": (now - timedelta(seconds=1)).isoformat(),
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
        }
    )

    list_resp = walker_session.get(f"{base_url}/api/walker/requests", timeout=30)
    assert list_resp.status_code == 200
    ignored = db.walker_requests.find_one({"id": late_req_id}, {"_id": 0})
    assert ignored and ignored.get("status") == "ignored"
    walker_after_ignore = db.users.find_one({"id": walker["id"]}, {"_id": 0})
    assert str(walker_after_ignore.get("match_last_penalty_reason", "")) in {"ignore", "reject"}
    walker_session.close()


def test_anti_spam_blocks_simultaneous_offers_to_same_walker(matching_env):
    db = matching_env["db"]
    base_url = matching_env["base_url"]
    client = _login(base_url, CLIENT_CREDS["email"], CLIENT_CREDS["password"])
    walk_date = (datetime.now(timezone.utc) + timedelta(days=10)).strftime("%Y-%m-%d")

    walker = _new_walker(matching_env, "antispam_single")
    _seed_good_history(db, walker)

    first = client.post(
        f"{base_url}/api/walks/matching-request",
        json=_matching_payload(walk_date=walk_date, walk_time="23:15"),
        timeout=30,
    )
    assert first.status_code == 201, first.text

    second = client.post(
        f"{base_url}/api/walks/matching-request",
        json=_matching_payload(walk_date=walk_date, walk_time="23:15"),
        timeout=30,
    )
    assert second.status_code == 404
    assert "Nenhum passeador elegível" in second.text
    client.close()
