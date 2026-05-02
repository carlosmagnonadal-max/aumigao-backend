from __future__ import annotations

import asyncio
import os
import uuid
from datetime import date, timedelta
from pathlib import Path

import pytest
import requests
from dotenv import dotenv_values
from pymongo import MongoClient


# Module coverage: auth playbook controls (bcrypt, cookies, CORS, lockout, seed admin update).
# Module coverage: walker performance system (goals, missions, ranking, levels, pricing, matching context).

WALKER_CREDS = {"email": "walker@petpasso.com", "password": "Walker@123"}
CLIENT_CREDS = {"email": "cliente@petpasso.com", "password": "Cliente@123"}
ADMIN_CREDS = {"email": "admin@petpasso.com", "password": "Admin@123"}
SUPER_ADMIN_CREDS = {"email": "superadmin@petpasso.com", "password": "SuperAdmin@123"}


def _mongo_db():
    backend_env = dotenv_values("/app/backend/.env")
    mongo_url = os.environ.get("MONGO_URL") or backend_env.get("MONGO_URL")
    db_name = os.environ.get("DB_NAME") or backend_env.get("DB_NAME")
    if not mongo_url or not db_name:
        pytest.skip("MONGO_URL/DB_NAME não configurados")
    client = MongoClient(str(mongo_url).strip().strip('"'))
    return client, client[str(db_name).strip().strip('"')]


def _target_date(delta_days: int = 2) -> str:
    return (date.today() + timedelta(days=delta_days)).strftime("%Y-%m-%d")


def _weekday_key(target_date: str) -> str:
    y, m, d = [int(part) for part in target_date.split("-")]
    return ["seg", "ter", "qua", "qui", "sex", "sab", "dom"][date(y, m, d).weekday()]


def _login(base_url: str, email: str, password: str) -> requests.Session:
    session = requests.Session()
    response = session.post(
        f"{base_url}/api/auth/login",
        json={"email": email, "password": password},
        timeout=30,
    )
    assert response.status_code == 200, f"Login falhou ({email}): {response.status_code} {response.text}"
    token = (response.json() or {}).get("access_token")
    assert token, f"Token ausente para {email}"
    session.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/json"})
    return session


def _ensure_client_pet(client: requests.Session, base_url: str) -> dict:
    pets_resp = client.get(f"{base_url}/api/pets", timeout=30)
    assert pets_resp.status_code == 200, pets_resp.text
    pets = pets_resp.json() if pets_resp.text else []
    assert pets, "Cliente de teste sem pets; esperado seed ativo"
    return pets[0]


def _common_matching_payload(client: requests.Session, base_url: str, walk_date: str, walk_time: str) -> dict:
    pet = _ensure_client_pet(client, base_url)
    return {
        "pet_name": pet["pet_name"],
        "pet_id": pet["id"],
        "client_name": "Cliente Demo",
        "walk_date": walk_date,
        "walk_time": walk_time,
        "duration_minutes": 30,
        "walk_type": "Individual",
        "tipo_passeio": "padrao",
        "modo_inicio_passeio": "endereco_tutor",
        "pickup_street": "Rua da Bahia",
        "pickup_number": "101",
        "pickup_neighborhood": "Pituba",
        "pickup_complement": "",
        "location_reference": "Próximo à praça",
        "pet_behavior_notes": "Calmo",
        "notes": f"TEST_ITER78_MATCH_{uuid.uuid4().hex[:8]}",
    }


def _is_critical_hour(clock_value: str) -> bool:
    hour = int(clock_value.split(":")[0])
    return (6 <= hour <= 9) or (16 <= hour <= 21)


def _find_viable_walker_and_time(client: requests.Session, base_url: str, target_date: str) -> tuple[str, str]:
    base_rows_resp = client.get(
        f"{base_url}/api/walkers",
        params={"date": target_date, "duration_minutes": 30, "neighborhood": "Pituba", "tipo_passeio": "padrao"},
        timeout=30,
    )
    assert base_rows_resp.status_code == 200, base_rows_resp.text
    base_rows = base_rows_resp.json()
    if not base_rows:
        pytest.skip("Ambiente sem passeadores elegíveis para a data alvo")

    for row in base_rows:
        walker_id = str(row.get("id") or "")
        if not walker_id:
            continue
        slots_resp = client.get(
            f"{base_url}/api/walkers/{walker_id}/availability-slots",
            params={"date": target_date, "duration_minutes": 30},
            timeout=30,
        )
        if slots_resp.status_code != 200:
            continue
        slots = slots_resp.json().get("available_slots") or []
        for slot in slots:
            if isinstance(slot, str) and _is_critical_hour(slot):
                return walker_id, slot

    pytest.skip("Nenhum horário crítico disponível entre passeadores elegíveis no ambiente")


def test_auth_playbook_bcrypt_hash_starts_with_2b():
    mongo_client, db = _mongo_db()
    try:
        admin_row = db.users.find_one({"email": ADMIN_CREDS["email"]}, {"_id": 0, "password_hash": 1})
        assert admin_row is not None
        assert str(admin_row.get("password_hash") or "").startswith("$2b$")
    finally:
        mongo_client.close()


def test_auth_playbook_login_sets_http_only_cookies(base_url: str):
    response = requests.post(
        f"{base_url}/api/auth/login",
        json=CLIENT_CREDS,
        timeout=30,
    )
    assert response.status_code == 200, response.text
    cookie_header = "\n".join(response.raw.headers.get_all("Set-Cookie") if response.raw and response.raw.headers else [response.headers.get("set-cookie", "")])
    assert "access_token=" in cookie_header
    assert "refresh_token=" in cookie_header
    assert "HttpOnly" in cookie_header


def test_auth_playbook_cors_allows_credentials_with_explicit_origin(base_url: str):
    preflight = requests.options(
        f"{base_url}/api/auth/login",
        headers={
            "Origin": base_url,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
        timeout=30,
    )
    assert preflight.status_code in {200, 204}
    assert preflight.headers.get("access-control-allow-credentials") == "true"
    assert preflight.headers.get("access-control-allow-origin") == base_url


def test_auth_playbook_bruteforce_lockout_after_five_fails(base_url: str):
    unique_ip = f"198.51.100.{int(uuid.uuid4().hex[:2], 16)}"
    statuses = []
    for _ in range(6):
        resp = requests.post(
            f"{base_url}/api/auth/login",
            json={"email": ADMIN_CREDS["email"], "password": "SenhaErrada!123"},
            headers={"x-forwarded-for": unique_ip},
            timeout=30,
        )
        statuses.append(resp.status_code)
    assert statuses[:5] == [401, 401, 401, 401, 401]
    assert statuses[5] == 429


def test_auth_playbook_seed_admin_updates_existing_password_if_changed():
    backend_path = Path("/app/backend")
    if str(backend_path) not in os.sys.path:
        os.sys.path.append(str(backend_path))

    from server import _hash_password, _verify_password, seed_auth_and_indexes  # noqa: WPS433

    mongo_client, db = _mongo_db()
    try:
        row = db.users.find_one({"email": ADMIN_CREDS["email"]}, {"_id": 0, "id": 1, "password_hash": 1})
        if not row:
            pytest.skip("Admin seed não encontrado")

        admin_id = str(row["id"])
        original_hash = str(row.get("password_hash") or "")
        db.users.update_one({"id": admin_id}, {"$set": {"password_hash": _hash_password("TmpWrong@123")}})

        asyncio.run(seed_auth_and_indexes())

        refreshed = db.users.find_one({"id": admin_id}, {"_id": 0, "password_hash": 1})
        assert refreshed and _verify_password(ADMIN_CREDS["password"], str(refreshed.get("password_hash") or ""))

        if original_hash:
            db.users.update_one({"id": admin_id}, {"$set": {"password_hash": original_hash}})
    finally:
        mongo_client.close()


def test_walker_incentives_summary_contains_goal_shortfall_missions_ranking_levels(base_url: str):
    walker = _login(base_url, WALKER_CREDS["email"], WALKER_CREDS["password"])
    try:
        response = walker.get(f"{base_url}/api/walker/incentives/summary", timeout=30)
        assert response.status_code == 200, response.text
        body = response.json()

        assert body.get("walker_level") in {"bronze", "silver", "gold", "prata", "ouro", "elite"}
        assert isinstance(body.get("level_progress_percent"), (int, float))
        assert isinstance(body.get("rides_to_next_bonus"), int)
        assert isinstance(body.get("earnings_to_next_bonus"), (int, float))
        assert isinstance(body.get("weekly_goal_progress_percent"), (int, float))
        assert isinstance(body.get("missions"), list)
        assert isinstance(body.get("ranking_week_position"), int)
        assert isinstance(body.get("ranking_month_position"), int)
        assert isinstance(body.get("ranking_week_top"), list)
        assert isinstance(body.get("ranking_month_top"), list)
        assert isinstance(body.get("mission_priority_points"), (int, float))
    finally:
        walker.close()


def test_walker_incentives_ranking_top_rows_have_expected_structure(base_url: str):
    walker = _login(base_url, WALKER_CREDS["email"], WALKER_CREDS["password"])
    try:
        response = walker.get(f"{base_url}/api/walker/incentives/summary", timeout=30)
        assert response.status_code == 200, response.text
        body = response.json()

        for key in ["ranking_week_top", "ranking_month_top"]:
            rows = body.get(key) or []
            for row in rows:
                assert isinstance(row.get("position"), int)
                assert isinstance(row.get("score"), (int, float))
                assert isinstance(row.get("completed_walks"), int)
                assert row.get("walker_level") in {"bronze", "silver", "gold", "prata", "ouro", "elite"}
    finally:
        walker.close()


def test_walkers_endpoint_exposes_dynamic_price_and_matching_fields(base_url: str):
    client = _login(base_url, CLIENT_CREDS["email"], CLIENT_CREDS["password"])
    try:
        target_date = _target_date(3)
        _, critical_slot = _find_viable_walker_and_time(client, base_url, target_date)
        response = client.get(
            f"{base_url}/api/walkers",
            params={"date": target_date, "duration_minutes": 30, "preferred_time": critical_slot, "neighborhood": "Pituba", "tipo_passeio": "padrao"},
            timeout=30,
        )
        assert response.status_code == 200, response.text
        rows = response.json()
        if not isinstance(rows, list) or not rows:
            pytest.skip("Sem passeadores no recorte exato após filtros de elegibilidade")

        top = rows[0]
        assert "dynamic_price_multiplier" in top
        assert "dynamic_price_reason" in top
        assert "mission_priority_points" in top
        assert "ranking_score_final" in top
        assert isinstance(top.get("dynamic_price_multiplier"), (int, float))
        assert str(top.get("dynamic_price_reason") or "") != ""
    finally:
        client.close()


def test_critical_hour_dynamic_pricing_contract(base_url: str):
    client = _login(base_url, CLIENT_CREDS["email"], CLIENT_CREDS["password"])
    try:
        target_date = _target_date(4)
        _, critical_slot = _find_viable_walker_and_time(client, base_url, target_date)
        response = client.get(
            f"{base_url}/api/walkers",
            params={"date": target_date, "duration_minutes": 30, "preferred_time": critical_slot, "neighborhood": "Pituba", "tipo_passeio": "padrao"},
            timeout=30,
        )
        assert response.status_code == 200, response.text
        rows = response.json()
        if not rows:
            pytest.skip("Sem passeadores para validar contrato de preço dinâmico")

        reason = str(rows[0].get("dynamic_price_reason") or "")
        multiplier = float(rows[0].get("dynamic_price_multiplier") or 1.0)
        assert reason in {"Baixa oferta em horário crítico", "Oferta moderada em horário crítico", "Preço padrão"}
        if reason != "Preço padrão":
            assert multiplier > 1.0
    finally:
        client.close()


def test_calendar_capacity_by_period_zero_blocks_availability_slots(base_url: str):
    walker = _login(base_url, WALKER_CREDS["email"], WALKER_CREDS["password"])
    client = _login(base_url, CLIENT_CREDS["email"], CLIENT_CREDS["password"])
    try:
        me = walker.get(f"{base_url}/api/auth/me", timeout=30)
        assert me.status_code == 200, me.text
        walker_user_id = str((me.json() or {}).get("id") or "")
        assert walker_user_id
        walker_public_id = f"partner-{walker_user_id}"

        current_settings = walker.get(f"{base_url}/api/walker/availability-settings", timeout=30)
        assert current_settings.status_code == 200, current_settings.text
        settings = current_settings.json()

        target_date = _target_date(5)
        weekday = _weekday_key(target_date)
        patch_payload = {
            "availability_days": [weekday],
            "availability_start_time": settings.get("availability_start_time", "08:00"),
            "availability_end_time": settings.get("availability_end_time", "18:00"),
            "availability_periods": settings.get("availability_periods", {}),
            "availability_capacity_by_period": {"manha": 0, "tarde": 0, "noite": 0},
        }
        patched = walker.patch(f"{base_url}/api/walker/availability", json=patch_payload, timeout=30)
        assert patched.status_code == 200, patched.text

        slots_resp = client.get(
            f"{base_url}/api/walkers/{walker_public_id}/availability-slots",
            params={"date": target_date, "duration_minutes": 30},
            timeout=30,
        )
        assert slots_resp.status_code == 200, slots_resp.text
        assert slots_resp.json().get("available_slots") == []
    finally:
        walker.close()
        client.close()


def test_matching_distribution_records_ranking_score_and_mission_priority(base_url: str):
    client = _login(base_url, CLIENT_CREDS["email"], CLIENT_CREDS["password"])
    mongo_client, db = _mongo_db()
    try:
        target_date = _target_date(6)
        _, critical_slot = _find_viable_walker_and_time(client, base_url, target_date)
        payload = _common_matching_payload(client, base_url, target_date, critical_slot)
        response = client.post(f"{base_url}/api/walks/matching-request", json=payload, timeout=30)
        if response.status_code == 404:
            pytest.skip("Sem candidatos elegíveis para matching no horário crítico do ambiente")
        assert response.status_code == 201, response.text
        row = response.json()
        matching_id = str(row.get("id") or "")
        assert matching_id

        stored = db.matching_requests.find_one({"id": matching_id}, {"_id": 0, "candidates": 1})
        assert stored is not None
        candidates = list(stored.get("candidates") or [])
        assert candidates, "Candidatos ausentes no matching request salvo"

        for item in candidates:
            assert isinstance(item.get("mission_priority_points"), (int, float))
            assert isinstance(item.get("ranking_score_final"), (int, float))

        ranking_scores = [float(item.get("ranking_score_final") or 0.0) for item in candidates]
        assert ranking_scores == sorted(ranking_scores, reverse=True)
    finally:
        mongo_client.close()
        client.close()
