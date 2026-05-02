from __future__ import annotations

from datetime import datetime, timedelta, timezone
import time

import pytest
import requests


# Module: auth/session role checks needed for protected dashboard flows.
# Module: pet praise feedback in finished walk for walker.
# Module: scheduling regression (create -> get persistence).


def _login(base_url: str, email: str, password: str) -> requests.Session:
    session = requests.Session()
    response = session.post(
        f"{base_url}/api/auth/login",
        json={"email": email, "password": password},
        timeout=30,
    )
    assert response.status_code == 200, f"Login falhou para {email}: {response.status_code} {response.text}"
    payload = response.json() or {}
    token = payload.get("access_token")
    assert token, "access_token ausente no login"
    session.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/json"})
    return session


def _tomorrow() -> str:
    return (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")


def _create_test_pet(client: requests.Session, base_url: str) -> dict:
    payload = {
        "pet_name": f"TEST_ITER74_{int(time.time())}",
        "behavioral_notes": "TEST pet amistoso",
        "photo_url": "",
        "owner_name": "TEST Cliente",
        "gets_along_with_dogs": True,
        "accepts_shared_walk": True,
        "pet_size": "Médio",
        "energy_level": "Médio",
        "pulls_leash": False,
        "dog_behavior": "Neutro",
    }
    response = client.post(f"{base_url}/api/pets", json=payload, timeout=30)
    assert response.status_code == 201, response.text
    pet = response.json()
    assert pet.get("id")
    assert str(pet.get("pet_name") or "").startswith("TEST_ITER74_")
    return pet


def _pick_walker(client: requests.Session, base_url: str) -> str:
    response = client.get(
        f"{base_url}/api/walkers",
        params={
            "date": _tomorrow(),
            "duration_minutes": 30,
            "preferred_time": "09:00",
            "tipo_passeio": "padrao",
        },
        timeout=30,
    )
    assert response.status_code == 200, response.text
    rows = response.json()
    assert isinstance(rows, list) and rows, "Nenhum passeador disponível"

    preferred = next((r for r in rows if str(r.get("name") or "").strip().lower() == "carlos oliveira"), None)
    selected = preferred or rows[0]
    walker_id = str(selected.get("id") or "").strip()
    assert walker_id
    return walker_id


def _create_walk(client: requests.Session, base_url: str, pet: dict, walker_id: str, notes: str) -> dict:
    payload = {
        "pet_name": pet["pet_name"],
        "pet_id": pet["id"],
        "client_name": "Cliente Demo",
        "walk_date": _tomorrow(),
        "walk_time": "09:00",
        "duration_minutes": 30,
        "walk_type": "Individual",
        "tipo_passeio": "padrao",
        "modo_inicio_passeio": "endereco_tutor",
        "walker_id": walker_id,
        "pickup_street": "Rua TEST",
        "pickup_number": "100",
        "pickup_neighborhood": "Pituba",
        "pickup_complement": "Apto TEST",
        "location_reference": "Perto da praça",
        "pet_behavior_notes": "TEST comportamento",
        "notes": notes,
    }
    response = client.post(f"{base_url}/api/walks", json=payload, timeout=30)
    assert response.status_code == 201, response.text
    walk = response.json()
    assert walk.get("id")
    assert walk.get("status") == "Agendado"
    return walk


def test_admin_dashboard_blocks_non_admin_and_allows_admin(base_url: str):
    admin = _login(base_url, "admin@petpasso.com", "Admin@123")
    client = _login(base_url, "cliente@petpasso.com", "Cliente@123")
    walker = _login(base_url, "walker@petpasso.com", "Walker@123")
    try:
        admin_response = admin.get(f"{base_url}/api/admin/dashboard", timeout=30)
        assert admin_response.status_code == 200, admin_response.text
        dashboard = admin_response.json()
        assert "total_walks_finished" in dashboard
        assert "weekly_tips_amount" in dashboard

        client_response = client.get(f"{base_url}/api/admin/dashboard", timeout=30)
        assert client_response.status_code == 403, client_response.text

        walker_response = walker.get(f"{base_url}/api/admin/dashboard", timeout=30)
        assert walker_response.status_code == 403, walker_response.text
    finally:
        admin.close()
        client.close()
        walker.close()


def test_walker_positive_praise_allowed_for_finished_walk(base_url: str):
    client = _login(base_url, "cliente@petpasso.com", "Cliente@123")
    admin = _login(base_url, "admin@petpasso.com", "Admin@123")
    walker = _login(base_url, "walker@petpasso.com", "Walker@123")
    try:
        pet = _create_test_pet(client, base_url)
        walker_id = _pick_walker(client, base_url)
        walk = _create_walk(client, base_url, pet, walker_id, notes="TEST_ITER74_PRAISE_FLOW")

        finish = admin.patch(
            f"{base_url}/api/admin/walks/{walk['id']}/status",
            json={"status": "Finalizado"},
            timeout=30,
        )
        assert finish.status_code == 200, finish.text
        finished = finish.json()
        assert finished.get("status") == "Finalizado"

        praise_response = walker.post(
            f"{base_url}/api/pets/{pet['id']}/praise-tags",
            json={"walk_id": walk["id"], "tags": ["docil", "ativo"]},
            timeout=30,
        )
        assert praise_response.status_code == 201, praise_response.text
        praise = praise_response.json()
        assert praise.get("pet_id") == pet["id"]
        assert praise.get("walk_id") == walk["id"]
        assert set(praise.get("tags") or []) == {"docil", "ativo"}
    finally:
        client.close()
        admin.close()
        walker.close()


def test_schedule_create_then_get_persists_main_flow(base_url: str):
    client = _login(base_url, "cliente@petpasso.com", "Cliente@123")
    try:
        pet = _create_test_pet(client, base_url)
        walker_id = _pick_walker(client, base_url)
        walk = _create_walk(client, base_url, pet, walker_id, notes="TEST_ITER74_SCHEDULE_REGRESSION")

        get_response = client.get(f"{base_url}/api/walks/{walk['id']}", timeout=30)
        assert get_response.status_code == 200, get_response.text
        persisted = get_response.json()
        assert persisted.get("id") == walk["id"]
        assert persisted.get("pet_name") == pet["pet_name"]
        assert persisted.get("status") == "Agendado"
    finally:
        client.close()


def test_walker_dashboard_endpoint_access_by_role(base_url: str):
    walker = _login(base_url, "walker@petpasso.com", "Walker@123")
    client = _login(base_url, "cliente@petpasso.com", "Cliente@123")
    try:
        walker_response = walker.get(f"{base_url}/api/walker/tasks", timeout=30)
        assert walker_response.status_code == 200, walker_response.text
        assert isinstance(walker_response.json(), list)

        client_response = client.get(f"{base_url}/api/walker/tasks", timeout=30)
        assert client_response.status_code == 403, client_response.text
    finally:
        walker.close()
        client.close()
