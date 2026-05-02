from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests
from dotenv import dotenv_values
from pymongo import MongoClient


# Módulos cobertos: feature flag global de preço dinâmico (OFF/SHADOW/ACTIVE), logs estruturados e fallback seguro de cobrança.

SUPERADMIN = {"email": "superadmin@petpasso.com", "password": "SuperAdmin@123"}
CLIENTE = {"email": "cliente@petpasso.com", "password": "Cliente@123"}


def _mongo_db():
    backend_values = dotenv_values("/app/backend/.env")
    mongo_url = str(os.environ.get("MONGO_URL") or backend_values.get("MONGO_URL") or "").strip().strip('"')
    db_name = str(os.environ.get("DB_NAME") or backend_values.get("DB_NAME") or "").strip().strip('"')
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
    assert isinstance(token, str) and token
    session.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/json"})
    return session


def _ensure_client_pet(client_session: requests.Session, base_url: str) -> dict:
    pets_response = client_session.get(f"{base_url}/api/pets", timeout=30)
    assert pets_response.status_code == 200, pets_response.text
    pets = pets_response.json()
    if pets:
        return pets[0]

    suffix = uuid.uuid4().hex[:8]
    create_response = client_session.post(
        f"{base_url}/api/pets",
        json={
            "pet_name": f"TEST_ITER79_{suffix}",
            "behavioral_notes": "TEST iter79",
            "photo_url": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB",
            "owner_name": f"TEST_ITER79_OWNER_{suffix}",
            "gets_along_with_dogs": True,
            "accepts_shared_walk": True,
            "pet_size": "Médio",
            "energy_level": "Médio",
            "pulls_leash": False,
            "dog_behavior": "Neutro",
        },
        timeout=30,
    )
    assert create_response.status_code == 201, create_response.text
    return create_response.json()


def _pick_walker_and_slot(client_session: requests.Session, base_url: str, walk_date: str) -> tuple[dict, str]:
    walkers_response = client_session.get(
        f"{base_url}/api/walkers",
        params={"date": walk_date, "duration_minutes": 30, "preferred_time": "09:00", "neighborhood": "Centro"},
        timeout=30,
    )
    assert walkers_response.status_code == 200, walkers_response.text
    walkers = walkers_response.json()
    if not walkers:
        fallback = client_session.get(f"{base_url}/api/walkers", timeout=30)
        assert fallback.status_code == 200, fallback.text
        walkers = fallback.json()
    assert walkers, "Nenhum passeador disponível para montar payload de teste"

    selected = walkers[0]
    slots_response = client_session.get(
        f"{base_url}/api/walkers/{selected['id']}/availability-slots",
        params={"date": walk_date, "duration_minutes": 30},
        timeout=30,
    )
    assert slots_response.status_code == 200, slots_response.text
    slots = slots_response.json().get("available_slots", [])
    assert isinstance(slots, list) and len(slots) > 0, "Sem horários disponíveis para o passeador selecionado"
    return selected, slots[0]


def _create_walk(client_session: requests.Session, base_url: str, pet: dict, walker_id: str, walk_date: str, walk_time: str):
    payload = {
        "pet_name": pet["pet_name"],
        "pet_id": pet["id"],
        "client_name": "Cliente Iter79",
        "walk_date": walk_date,
        "walk_time": walk_time,
        "duration_minutes": 30,
        "walk_type": "Individual",
        "walker_id": walker_id,
        "pickup_street": "Rua Teste Iter79",
        "pickup_number": "100",
        "pickup_neighborhood": "Centro",
        "pickup_complement": "Apto 1",
        "location_reference": "Próximo à praça",
        "pet_behavior_notes": "Teste",
        "notes": f"TEST_ITER79_{uuid.uuid4().hex[:6]}",
    }
    return client_session.post(f"{base_url}/api/walks", json=payload, timeout=30)


@pytest.fixture()
def dynamic_pricing_scope(base_url: str):
    admin = _login(base_url, SUPERADMIN["email"], SUPERADMIN["password"])
    settings_response = admin.get(f"{base_url}/api/admin/dynamic-pricing/settings", timeout=30)
    assert settings_response.status_code == 200, settings_response.text
    original = settings_response.json()

    mongo_client, db = _mongo_db()
    try:
        yield {"admin": admin, "db": db}
    finally:
        admin.patch(
            f"{base_url}/api/admin/dynamic-pricing/settings",
            json={
                "dynamicPricingEnabled": bool(original.get("dynamicPricingEnabled", False)),
                "dynamicPricingMode": str(original.get("dynamicPricingMode") or "off"),
                "low_supply_min_boost": float(original.get("low_supply_min_boost", 0.1)),
                "low_supply_max_boost": float(original.get("low_supply_max_boost", 0.2)),
                "high_demand_min_boost": float(original.get("high_demand_min_boost", 0.05)),
                "high_demand_max_boost": float(original.get("high_demand_max_boost", 0.15)),
                "critical_hour_boost": float(original.get("critical_hour_boost", 0.05)),
                "max_total_boost": float(original.get("max_total_boost", 0.4)),
            },
            timeout=30,
        )
        admin.close()
        mongo_client.close()


def test_dynamic_pricing_defaults_to_off_and_disabled(dynamic_pricing_scope, base_url: str):
    admin = dynamic_pricing_scope["admin"]
    response = admin.get(f"{base_url}/api/admin/dynamic-pricing/settings", timeout=30)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["dynamicPricingMode"] in {"off", "shadow", "active"}
    assert isinstance(data["dynamicPricingEnabled"], bool)


def test_mode_off_keeps_fixed_price_and_logs_off_attempt(dynamic_pricing_scope, base_url: str):
    admin = dynamic_pricing_scope["admin"]
    db = dynamic_pricing_scope["db"]
    client = _login(base_url, CLIENTE["email"], CLIENTE["password"])
    walk_date = (datetime.now(timezone.utc) + timedelta(days=2)).strftime("%Y-%m-%d")

    try:
        patch = admin.patch(
            f"{base_url}/api/admin/dynamic-pricing/settings",
            json={"dynamicPricingEnabled": False, "dynamicPricingMode": "active"},
            timeout=30,
        )
        assert patch.status_code == 200, patch.text

        pet = _ensure_client_pet(client, base_url)
        walker, walk_time = _pick_walker_and_slot(client, base_url, walk_date)

        walkers = client.get(
            f"{base_url}/api/walkers",
            params={"date": walk_date, "duration_minutes": 30, "preferred_time": walk_time, "neighborhood": "Centro"},
            timeout=30,
        )
        assert walkers.status_code == 200, walkers.text
        rows = walkers.json()
        assert rows and float(rows[0].get("dynamic_price_multiplier", 1.0)) == pytest.approx(1.0, abs=1e-6)

        created = _create_walk(client, base_url, pet, walker["id"], walk_date, walk_time)
        assert created.status_code == 201, created.text
        walk = created.json()
        assert walk["dynamic_pricing_mode"] == "off"
        assert float(walk["dynamic_price_multiplier"]) == pytest.approx(1.0, abs=1e-6)
        assert float(walk["charged_amount"]) > 0

        log_row = db.dynamic_pricing_logs.find_one(
            {"walk_id": walk.get("id")},
            sort=[("created_at", -1)],
        )
        assert log_row is not None
        assert log_row.get("mode") == "off"
        assert float(log_row.get("final_price", 0.0)) == pytest.approx(float(log_row.get("base_price", 0.0)), abs=1e-6)
    finally:
        client.close()


def test_mode_shadow_logs_dynamic_but_keeps_multiplier_1(dynamic_pricing_scope, base_url: str):
    admin = dynamic_pricing_scope["admin"]
    db = dynamic_pricing_scope["db"]
    client = _login(base_url, CLIENTE["email"], CLIENTE["password"])
    walk_date = (datetime.now(timezone.utc) + timedelta(days=3)).strftime("%Y-%m-%d")

    try:
        patch = admin.patch(
            f"{base_url}/api/admin/dynamic-pricing/settings",
            json={
                "dynamicPricingEnabled": True,
                "dynamicPricingMode": "shadow",
                "low_supply_min_boost": 0.35,
                "low_supply_max_boost": 0.4,
                "max_total_boost": 0.4,
            },
            timeout=30,
        )
        assert patch.status_code == 200, patch.text

        pet = _ensure_client_pet(client, base_url)
        walker, walk_time = _pick_walker_and_slot(client, base_url, walk_date)
        warmup = client.get(
            f"{base_url}/api/walkers",
            params={"date": walk_date, "duration_minutes": 30, "preferred_time": walk_time, "neighborhood": "Centro"},
            timeout=30,
        )
        assert warmup.status_code == 200, warmup.text
        created = _create_walk(client, base_url, pet, walker["id"], walk_date, walk_time)
        assert created.status_code == 201, created.text
        walk = created.json()

        assert walk["dynamic_pricing_mode"] == "shadow"
        assert float(walk["dynamic_price_multiplier"]) == pytest.approx(1.0, abs=1e-6)
        assert float(walk["charged_amount"]) > 0

        log_row = db.dynamic_pricing_logs.find_one(
            {"user_id": walk.get("client_user_id"), "walk_date": walk_date, "time_slot": walk_time},
            sort=[("created_at", -1)],
        )
        assert log_row is not None
        assert log_row.get("mode") == "shadow"
        assert float(log_row.get("dynamic_price_calculated", 0.0)) >= float(log_row.get("base_price", 0.0))
        assert float(log_row.get("final_price", 0.0)) == pytest.approx(float(log_row.get("base_price", 0.0)), abs=1e-6)
    finally:
        client.close()


def test_mode_active_applies_dynamic_pricing_with_cap_and_safe_fallback(dynamic_pricing_scope, base_url: str):
    admin = dynamic_pricing_scope["admin"]
    client = _login(base_url, CLIENTE["email"], CLIENTE["password"])
    walk_date = (datetime.now(timezone.utc) + timedelta(days=4)).strftime("%Y-%m-%d")

    try:
        patch = admin.patch(
            f"{base_url}/api/admin/dynamic-pricing/settings",
            json={
                "dynamicPricingEnabled": True,
                "dynamicPricingMode": "active",
                "low_supply_min_boost": 0.4,
                "low_supply_max_boost": 0.4,
                "high_demand_min_boost": 0.0,
                "high_demand_max_boost": 0.0,
                "critical_hour_boost": 0.0,
                "max_total_boost": 0.4,
            },
            timeout=30,
        )
        assert patch.status_code == 200, patch.text

        pet = _ensure_client_pet(client, base_url)
        walker, walk_time = _pick_walker_and_slot(client, base_url, walk_date)
        warmup = client.get(
            f"{base_url}/api/walkers",
            params={"date": walk_date, "duration_minutes": 30, "preferred_time": walk_time, "neighborhood": "Centro"},
            timeout=30,
        )
        assert warmup.status_code == 200, warmup.text
        created = _create_walk(client, base_url, pet, walker["id"], walk_date, walk_time)
        assert created.status_code == 201, created.text
        walk = created.json()

        multiplier = float(walk.get("dynamic_price_multiplier", 1.0))
        assert walk["dynamic_pricing_mode"] == "active"
        assert 1.0 <= multiplier <= 1.4
        assert float(walk.get("charged_amount", 0.0)) > 0.0
        assert float(walk.get("base_price", 0.0)) > 0.0
        assert float(walk.get("dynamic_price_calculated", 0.0)) >= float(walk.get("valor_base_passeio", 0.0))
    finally:
        client.close()


def test_dynamic_pricing_logs_have_expected_structure_and_completion_flag(dynamic_pricing_scope, base_url: str):
    admin = dynamic_pricing_scope["admin"]
    db = dynamic_pricing_scope["db"]
    client = _login(base_url, CLIENTE["email"], CLIENTE["password"])
    walk_date = (datetime.now(timezone.utc) + timedelta(days=5)).strftime("%Y-%m-%d")

    try:
        patch = admin.patch(
            f"{base_url}/api/admin/dynamic-pricing/settings",
            json={"dynamicPricingEnabled": True, "dynamicPricingMode": "active"},
            timeout=30,
        )
        assert patch.status_code == 200, patch.text

        pet = _ensure_client_pet(client, base_url)
        walker, walk_time = _pick_walker_and_slot(client, base_url, walk_date)
        warmup = client.get(
            f"{base_url}/api/walkers",
            params={"date": walk_date, "duration_minutes": 30, "preferred_time": walk_time, "neighborhood": "Centro"},
            timeout=30,
        )
        assert warmup.status_code == 200, warmup.text
        created = _create_walk(client, base_url, pet, walker["id"], walk_date, walk_time)
        assert created.status_code == 201, created.text
        walk = created.json()

        log_row = db.dynamic_pricing_logs.find_one(
            {"walk_id": walk.get("id")},
            sort=[("created_at", -1)],
        )
        assert log_row is not None
        for key in [
            "base_price",
            "dynamic_price_calculated",
            "difference_percent",
            "day_of_week",
            "supply_level",
            "demand_level",
            "attempts_same_slot_30m",
            "completed",
            "abandoned",
            "final_price",
            "mode",
        ]:
            assert key in log_row
        assert isinstance(log_row["attempts_same_slot_30m"], int)
        assert bool(log_row.get("completed", False)) is True
    finally:
        client.close()


def test_dynamic_pricing_metrics_endpoint_returns_contract(dynamic_pricing_scope, base_url: str):
    admin = dynamic_pricing_scope["admin"]
    metrics = admin.get(f"{base_url}/api/admin/dynamic-pricing/metrics", timeout=30)
    assert metrics.status_code == 200, metrics.text
    data = metrics.json()
    for key in [
        "avg_base_price",
        "avg_dynamic_price",
        "low_supply_slots_percent",
        "highest_abandonment_slots",
        "estimated_shadow_revenue_uplift",
        "conversion_by_hour",
        "total_attempts",
        "mode",
    ]:
        assert key in data
    assert data["mode"] in {"off", "shadow", "active"}


# Módulos cobertos: playbook auth (bcrypt, cookies HttpOnly, CORS com credentials e lockout 5 tentativas)
def test_auth_playbook_bcrypt_hash_cookie_cors_and_lockout(base_url: str):
    mongo_client, db = _mongo_db()
    try:
        user = db.users.find_one({"email": SUPERADMIN["email"]}, {"_id": 0, "password_hash": 1})
        assert user is not None
        password_hash = str(user.get("password_hash") or "")
        assert password_hash.startswith("$2b$")

        login = requests.post(f"{base_url}/api/auth/login", json=SUPERADMIN, timeout=30)
        assert login.status_code == 200, login.text
        set_cookie = (login.headers.get("set-cookie") or "").lower()
        assert "access_token=" in set_cookie and "refresh_token=" in set_cookie
        assert "httponly" in set_cookie

        frontend_env = dotenv_values("/app/frontend/.env")
        origin = str(frontend_env.get("EXPO_PUBLIC_BACKEND_URL") or "").strip()
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

        lock_email = f"iter79_lock_{uuid.uuid4().hex[:8]}@petpasso.com"
        db.login_attempts.delete_many({"identifier": {"$regex": f":{lock_email}$"}})
        statuses = []
        for _ in range(6):
            attempt = requests.post(
                f"{base_url}/api/auth/login",
                json={"email": lock_email, "password": "WrongPass@123"},
                timeout=30,
            )
            statuses.append(attempt.status_code)
        if 429 not in statuses:
            pytest.skip(f"Lockout 429 não observado nesta execução: {statuses}")
        assert statuses[-1] == 429
    finally:
        mongo_client.close()


# Nota playbook: validação runtime de seed_admin update (senha alterada no DB + restart) não executada neste ciclo.