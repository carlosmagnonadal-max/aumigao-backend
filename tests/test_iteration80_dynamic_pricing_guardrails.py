from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import requests
from dotenv import dotenv_values
from pymongo import MongoClient


# Módulos cobertos: guardrails de preço dinâmico (OFF/SHADOW/ACTIVE, teto +20%, smoothing ±10%, auto-calibração, snapshots/rollback e consistência de cobrança).

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
            "pet_name": f"TEST_ITER80_{suffix}",
            "behavioral_notes": "TEST iter80",
            "photo_url": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB",
            "owner_name": f"TEST_ITER80_OWNER_{suffix}",
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


def _pick_slot_for_walker(client_session: requests.Session, base_url: str, walker_id: str, walk_date: str) -> str:
    slots_response = client_session.get(
        f"{base_url}/api/walkers/{walker_id}/availability-slots",
        params={"date": walk_date, "duration_minutes": 30},
        timeout=30,
    )
    assert slots_response.status_code == 200, slots_response.text
    slots = slots_response.json().get("available_slots", [])
    assert isinstance(slots, list) and len(slots) > 0, "Sem horários para o passeador selecionado"
    return slots[0]


def _create_walk(client_session: requests.Session, base_url: str, pet: dict, walker_id: str, walk_date: str, walk_time: str):
    payload = {
        "pet_name": pet["pet_name"],
        "pet_id": pet["id"],
        "client_name": "Cliente Iter80",
        "walk_date": walk_date,
        "walk_time": walk_time,
        "duration_minutes": 30,
        "walk_type": "Individual",
        "walker_id": walker_id,
        "pickup_street": "Rua Teste Iter80",
        "pickup_number": "100",
        "pickup_neighborhood": "Centro",
        "pickup_complement": "Apto 1",
        "location_reference": "Próximo à praça",
        "pet_behavior_notes": "Teste",
        "notes": f"TEST_ITER80_{uuid.uuid4().hex[:6]}",
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
                "max_total_boost": float(original.get("max_total_boost", 0.2)),
                "smoothing_limit": float(original.get("smoothing_limit", 0.1)),
                "max_price_cap": float(original.get("max_price_cap", 40.0)),
                "auto_calibration_enabled": bool(original.get("auto_calibration_enabled", False)),
                "manual_lock": bool(original.get("manual_lock", False)),
            },
            timeout=30,
        )
        admin.close()
        mongo_client.close()


def test_default_flags_start_off(dynamic_pricing_scope, base_url: str):
    admin = dynamic_pricing_scope["admin"]
    response = admin.get(f"{base_url}/api/admin/dynamic-pricing/settings", timeout=30)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["dynamicPricingEnabled"] is False
    assert data["dynamicPricingMode"] == "off"


def test_mode_off_shadow_active_behaviour(dynamic_pricing_scope, base_url: str):
    admin = dynamic_pricing_scope["admin"]
    client = _login(base_url, CLIENTE["email"], CLIENTE["password"])
    walk_date = (datetime.now(timezone.utc) + timedelta(days=2)).strftime("%Y-%m-%d")

    try:
        pet = _ensure_client_pet(client, base_url)
        walker, walk_time = _pick_walker_and_slot(client, base_url, walk_date)

        off_patch = admin.patch(
            f"{base_url}/api/admin/dynamic-pricing/settings",
            json={"dynamicPricingEnabled": False, "dynamicPricingMode": "active"},
            timeout=30,
        )
        assert off_patch.status_code == 200, off_patch.text
        off_walk = _create_walk(client, base_url, pet, walker["id"], walk_date, walk_time)
        assert off_walk.status_code == 201, off_walk.text
        off_data = off_walk.json()
        assert off_data["dynamic_pricing_mode"] == "off"
        assert float(off_data["dynamic_price_multiplier"]) == pytest.approx(1.0, abs=1e-6)

        shadow_patch = admin.patch(
            f"{base_url}/api/admin/dynamic-pricing/settings",
            json={"dynamicPricingEnabled": True, "dynamicPricingMode": "shadow"},
            timeout=30,
        )
        assert shadow_patch.status_code == 200, shadow_patch.text
        shadow_slot = _pick_slot_for_walker(client, base_url, walker["id"], walk_date)
        shadow_walk = _create_walk(client, base_url, pet, walker["id"], walk_date, shadow_slot)
        assert shadow_walk.status_code == 201, shadow_walk.text
        shadow_data = shadow_walk.json()
        assert shadow_data["dynamic_pricing_mode"] == "shadow"
        assert float(shadow_data["dynamic_price_multiplier"]) == pytest.approx(1.0, abs=1e-6)

        active_patch = admin.patch(
            f"{base_url}/api/admin/dynamic-pricing/settings",
            json={"dynamicPricingEnabled": True, "dynamicPricingMode": "active"},
            timeout=30,
        )
        assert active_patch.status_code == 200, active_patch.text
        active_slot = _pick_slot_for_walker(client, base_url, walker["id"], walk_date)
        active_walk = _create_walk(client, base_url, pet, walker["id"], walk_date, active_slot)
        assert active_walk.status_code == 201, active_walk.text
        active_data = active_walk.json()
        assert active_data["dynamic_pricing_mode"] == "active"
        assert float(active_data["dynamic_price_multiplier"]) >= 1.0
    finally:
        client.close()


def test_max_total_boost_is_conservative_20_percent(dynamic_pricing_scope, base_url: str):
    admin = dynamic_pricing_scope["admin"]
    response = admin.patch(
        f"{base_url}/api/admin/dynamic-pricing/settings",
        json={"dynamicPricingEnabled": True, "dynamicPricingMode": "active", "max_total_boost": 0.4},
        timeout=30,
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert float(data["max_total_boost"]) <= 0.2


def test_smoothing_limit_caps_hourly_change_at_10_percent(dynamic_pricing_scope, base_url: str):
    admin = dynamic_pricing_scope["admin"]
    db = dynamic_pricing_scope["db"]
    client = _login(base_url, CLIENTE["email"], CLIENTE["password"])
    walk_date = (datetime.now(timezone.utc) + timedelta(days=3)).strftime("%Y-%m-%d")

    try:
        patch = admin.patch(
            f"{base_url}/api/admin/dynamic-pricing/settings",
            json={
                "dynamicPricingEnabled": True,
                "dynamicPricingMode": "active",
                "smoothing_limit": 0.1,
                "low_supply_min_boost": 0.2,
                "low_supply_max_boost": 0.2,
                "high_demand_min_boost": 0.15,
                "high_demand_max_boost": 0.15,
                "critical_hour_boost": 0.05,
                "max_total_boost": 0.2,
            },
            timeout=30,
        )
        assert patch.status_code == 200, patch.text

        now_iso = datetime.now(timezone.utc).isoformat()
        db.dynamic_pricing_logs.insert_one(
            {
                "id": str(uuid.uuid4()),
                "user_id": "seed-smoothing",
                "created_at": now_iso,
                "walk_date": walk_date,
                "time_slot": "08:15",
                "day_of_week": "seg",
                "base_price": 29.9,
                "dynamic_price_calculated": 32.89,
                "difference_percent": 10.0,
                "supply_level": 2,
                "demand_level": 8,
                "attempts_same_slot_30m": 1,
                "mode": "active",
                "final_price": 32.89,
                "completed": True,
                "abandoned": False,
                "walk_id": "seed-smoothing",
            }
        )

        walkers_response = client.get(
            f"{base_url}/api/walkers",
            params={"date": walk_date, "duration_minutes": 30, "preferred_time": "09:15", "neighborhood": "Centro"},
            timeout=30,
        )
        assert walkers_response.status_code == 200, walkers_response.text
        rows = walkers_response.json()
        assert rows
        # previous hour multiplier 1.10 + smoothing 0.10 => limite superior 1.20
        assert float(rows[0].get("dynamic_price_multiplier", 1.0)) <= 1.2
    finally:
        client.close()


def test_mvp_price_cap_around_40_brl(dynamic_pricing_scope, base_url: str):
    admin = dynamic_pricing_scope["admin"]
    response = admin.patch(
        f"{base_url}/api/admin/dynamic-pricing/settings",
        json={"max_price_cap": 80.0},
        timeout=30,
    )
    assert response.status_code == 200, response.text
    assert float(response.json()["max_price_cap"]) <= 40.0


def test_auto_calibration_runs_at_most_once_per_day(dynamic_pricing_scope, base_url: str):
    admin = dynamic_pricing_scope["admin"]
    db = dynamic_pricing_scope["db"]

    current = admin.get(f"{base_url}/api/admin/dynamic-pricing/settings", timeout=30)
    assert current.status_code == 200, current.text
    settings = current.json()

    settings["dynamicPricingEnabled"] = True
    settings["dynamicPricingMode"] = "active"
    settings["auto_calibration_enabled"] = True
    settings["manual_lock"] = False
    settings["calibration_min_events"] = 10
    settings["last_calibrated_at"] = datetime.now(timezone.utc).isoformat()
    db.dynamic_pricing_settings.update_one({"id": "default"}, {"$set": settings}, upsert=True)

    first_read = admin.get(f"{base_url}/api/admin/dynamic-pricing/settings", timeout=30)
    assert first_read.status_code == 200, first_read.text
    first_row = db.dynamic_pricing_settings.find_one({"id": "default"}, {"_id": 0}) or {}
    first_mark = str(first_row.get("last_calibrated_at") or "")

    second_read = admin.get(f"{base_url}/api/admin/dynamic-pricing/settings", timeout=30)
    assert second_read.status_code == 200, second_read.text
    second_row = db.dynamic_pricing_settings.find_one({"id": "default"}, {"_id": 0}) or {}
    second_mark = str(second_row.get("last_calibrated_at") or "")

    assert first_mark == second_mark


def test_calibration_not_triggered_outside_03_brt_window(dynamic_pricing_scope, base_url: str):
    admin = dynamic_pricing_scope["admin"]
    db = dynamic_pricing_scope["db"]

    current = admin.get(f"{base_url}/api/admin/dynamic-pricing/settings", timeout=30)
    assert current.status_code == 200, current.text
    settings = current.json()
    settings.update(
        {
            "dynamicPricingEnabled": True,
            "dynamicPricingMode": "active",
            "auto_calibration_enabled": True,
            "manual_lock": False,
            "calibration_min_events": 10,
            "last_calibrated_at": "",
            "last_conversion_rate": 0.8,
            "last_avg_revenue": 30.0,
        }
    )
    db.dynamic_pricing_settings.update_one({"id": "default"}, {"$set": settings}, upsert=True)

    now = datetime.now(timezone.utc)
    # população mínima de eventos para elegibilidade de calibração
    logs = []
    for i in range(12):
        logs.append(
            {
                "id": str(uuid.uuid4()),
                "user_id": f"iter80-cal-{i}",
                "created_at": (now - timedelta(minutes=i)).isoformat(),
                "walk_date": now.strftime("%Y-%m-%d"),
                "time_slot": "09:00",
                "day_of_week": "seg",
                "base_price": 29.9,
                "dynamic_price_calculated": 32.0,
                "difference_percent": 7.0,
                "supply_level": 2,
                "demand_level": 8,
                "attempts_same_slot_30m": 1,
                "mode": "active",
                "final_price": 29.9,
                "completed": i % 3 == 0,
                "abandoned": i % 3 != 0,
                "walk_id": None,
            }
        )
    db.dynamic_pricing_logs.insert_many(logs)

    read = admin.get(f"{base_url}/api/admin/dynamic-pricing/settings", timeout=30)
    assert read.status_code == 200, read.text
    data = read.json()
    db_row = db.dynamic_pricing_settings.find_one({"id": "default"}, {"_id": 0}) or {}

    # Regra de produto: calibrar apenas na janela de 03:00 BRT.
    now_brt_hour = (datetime.now(timezone.utc) + timedelta(hours=-3)).hour
    if now_brt_hour != 3:
        assert str(db_row.get("last_calibrated_at") or "") == ""


def test_logs_snapshots_and_rollback_contract(dynamic_pricing_scope, base_url: str):
    admin = dynamic_pricing_scope["admin"]
    client = _login(base_url, CLIENTE["email"], CLIENTE["password"])
    walk_date = (datetime.now(timezone.utc) + timedelta(days=4)).strftime("%Y-%m-%d")

    try:
        a = admin.patch(
            f"{base_url}/api/admin/dynamic-pricing/settings",
            json={"dynamicPricingEnabled": True, "dynamicPricingMode": "shadow"},
            timeout=30,
        )
        assert a.status_code == 200, a.text
        b = admin.patch(
            f"{base_url}/api/admin/dynamic-pricing/settings",
            json={"dynamicPricingEnabled": True, "dynamicPricingMode": "active", "auto_calibration_enabled": True, "manual_lock": True},
            timeout=30,
        )
        assert b.status_code == 200, b.text

        snaps = admin.get(f"{base_url}/api/admin/dynamic-pricing/snapshots?limit=5", timeout=30)
        assert snaps.status_code == 200, snaps.text
        rows = snaps.json()
        assert isinstance(rows, list) and rows
        latest = rows[0]
        for key in ["previous_settings", "new_settings", "reason", "impact_note", "created_at"]:
            assert key in latest

        rollback = admin.post(
            f"{base_url}/api/admin/dynamic-pricing/rollback",
            json={"snapshot_id": latest["id"]},
            timeout=30,
        )
        assert rollback.status_code == 200, rollback.text
        rollback_data = rollback.json()
        assert rollback_data["id"] == "default"

        pet = _ensure_client_pet(client, base_url)
        walker, walk_time = _pick_walker_and_slot(client, base_url, walk_date)
        created = _create_walk(client, base_url, pet, walker["id"], walk_date, walk_time)
        assert created.status_code == 201, created.text
        walk = created.json()

        expected_total = round(
            max(0.0, (float(walk.get("valor_base_passeio", 0.0)) + float(walk.get("adicionalDeslocamento", 0.0)))
                * float(walk.get("dynamic_price_multiplier", 1.0))
                - float(walk.get("discount_amount", 0.0))),
            2,
        )
        assert float(walk.get("charged_amount", 0.0)) == pytest.approx(expected_total, abs=1e-2)
    finally:
        client.close()
