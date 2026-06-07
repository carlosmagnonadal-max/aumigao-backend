from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import requests
from dotenv import dotenv_values
from pymongo import MongoClient


# Módulos cobertos: guardrails de configuração e consistência de confirmação de preço dinâmico em ACTIVE.

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
            "pet_name": f"TEST_ITER81_{suffix}",
            "behavioral_notes": "TEST iter81",
            "photo_url": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB",
            "owner_name": f"TEST_ITER81_OWNER_{suffix}",
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
    assert isinstance(slots, list) and len(slots) > 0, "Sem horários disponíveis"
    return selected, slots[0]


@pytest.fixture()
def pricing_scope(base_url: str):
    admin = _login(base_url, SUPERADMIN["email"], SUPERADMIN["password"])
    client = _login(base_url, CLIENTE["email"], CLIENTE["password"])

    settings_response = admin.get(f"{base_url}/api/admin/dynamic-pricing/settings", timeout=30)
    assert settings_response.status_code == 200, settings_response.text
    original = settings_response.json()

    mongo_client, db = _mongo_db()
    try:
        yield {"admin": admin, "client": client, "db": db}
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
                "max_total_boost": float(original.get("max_total_boost", 0.2)),
                "smoothing_limit": float(original.get("smoothing_limit", 0.1)),
                "max_price_cap": float(original.get("max_price_cap", 40.0)),
                "auto_calibration_enabled": bool(original.get("auto_calibration_enabled", False)),
                "manual_lock": bool(original.get("manual_lock", False)),
            },
            timeout=30,
        )
        client.close()
        admin.close()
        mongo_client.close()


def test_patch_settings_clamps_boost_and_cap(pricing_scope, base_url: str):
    admin = pricing_scope["admin"]
    response = admin.patch(
        f"{base_url}/api/admin/dynamic-pricing/settings",
        json={"dynamicPricingEnabled": True, "dynamicPricingMode": "active", "max_total_boost": 0.4, "max_price_cap": 80},
        timeout=30,
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert float(data["max_total_boost"]) <= 0.2
    assert float(data["max_price_cap"]) <= 40.0


def test_active_walk_persists_confirmed_dynamic_pricing_log_fields(pricing_scope, base_url: str):
    admin = pricing_scope["admin"]
    client = pricing_scope["client"]
    db = pricing_scope["db"]

    patch = admin.patch(
        f"{base_url}/api/admin/dynamic-pricing/settings",
        json={
            "dynamicPricingEnabled": True,
            "dynamicPricingMode": "active",
            "max_total_boost": 0.2,
            "max_price_cap": 40.0,
            "auto_calibration_enabled": False,
        },
        timeout=30,
    )
    assert patch.status_code == 200, patch.text

    pet = _ensure_client_pet(client, base_url)
    walk_date = (datetime.now(timezone.utc) + timedelta(days=2)).strftime("%Y-%m-%d")
    walker, walk_time = _pick_walker_and_slot(client, base_url, walk_date)

    preview_response = client.get(
        f"{base_url}/api/walkers",
        params={"date": walk_date, "duration_minutes": 30, "preferred_time": walk_time, "neighborhood": "Centro"},
        timeout=30,
    )
    assert preview_response.status_code == 200, preview_response.text

    payload = {
        "pet_name": pet["pet_name"],
        "pet_id": pet["id"],
        "client_name": "Cliente Iter81",
        "walk_date": walk_date,
        "walk_time": walk_time,
        "duration_minutes": 30,
        "walk_type": "Individual",
        "walker_id": walker["id"],
        "pickup_street": "Rua Teste Iter81",
        "pickup_number": "100",
        "pickup_neighborhood": "Centro",
        "pickup_complement": "Apto 1",
        "location_reference": "Próximo à praça",
        "pet_behavior_notes": "Teste",
        "notes": f"TEST_ITER81_{uuid.uuid4().hex[:6]}",
    }
    created = client.post(f"{base_url}/api/walks", json=payload, timeout=30)
    assert created.status_code == 201, created.text
    walk = created.json()

    assert float(walk.get("dynamic_price_multiplier", 1.0)) <= 1.2

    expected_total = round(
        max(
            0.0,
            (float(walk.get("valor_base_passeio", 0.0)) + float(walk.get("adicionalDeslocamento", 0.0)))
            * float(walk.get("dynamic_price_multiplier", 1.0))
            - float(walk.get("discount_amount", 0.0)),
        ),
        2,
    )
    assert float(walk.get("charged_amount", 0.0)) == pytest.approx(expected_total, abs=1e-2)

    log = db.dynamic_pricing_logs.find_one({"walk_id": walk["id"]}, {"_id": 0}, sort=[("created_at", -1)])
    assert log is not None, "Registro de dynamic_pricing para o walk confirmado não encontrado"
    assert float(log.get("confirmed_final_price", -1)) == pytest.approx(float(walk.get("charged_amount", 0.0)), abs=1e-2)
    assert float(log.get("confirmed_dynamic_multiplier", -1)) == pytest.approx(float(walk.get("dynamic_price_multiplier", 1.0)), abs=1e-4)
    assert isinstance(log.get("price_preview_vs_confirmed_consistent"), bool)
