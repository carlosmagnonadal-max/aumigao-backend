import uuid
from datetime import datetime, timedelta, timezone

import pytest
import requests


# Module coverage: tip quick amounts (2/5/10), tip summary contract, auth CORS/cookies/lockout


def _create_finished_walk(api_client: requests.Session, base_url: str, suffix: str) -> dict:
    walk_date = (datetime.now(timezone.utc) + timedelta(days=2)).strftime("%Y-%m-%d")
    slots_resp = api_client.get(
        f"{base_url}/api/walkers/walker-1/availability-slots",
        params={"date": walk_date, "duration_minutes": 30},
        timeout=25,
    )
    assert slots_resp.status_code == 200
    slots = slots_resp.json().get("available_slots", [])
    if not slots:
        pytest.skip("Sem horários disponíveis para walker-1")

    payload = {
        "pet_name": f"TEST_ITER38_Pet_{suffix}",
        "client_name": f"TEST_ITER38_Client_{suffix}",
        "walk_date": walk_date,
        "walk_time": str(slots[0]),
        "duration_minutes": 30,
        "walk_type": "Individual",
        "walker_id": "walker-1",
        "pickup_street": "Rua Teste",
        "pickup_number": "100",
        "pickup_neighborhood": "Centro",
        "pickup_complement": "",
        "location_reference": "TEST_ITER38",
        "notes": "TEST_ITER38",
    }
    created = api_client.post(f"{base_url}/api/walks", json=payload, timeout=25)
    assert created.status_code == 201
    walk = created.json()

    for next_status in ["Indo buscar o pet", "Passeando agora", "Finalizado"]:
        status_resp = api_client.patch(
            f"{base_url}/api/walks/{walk['id']}/status",
            json={"status": next_status},
            timeout=25,
        )
        assert status_resp.status_code == 200

    persisted = api_client.get(f"{base_url}/api/walks/{walk['id']}", timeout=25)
    assert persisted.status_code == 200
    return persisted.json()


@pytest.mark.parametrize("quick_amount", [2, 5, 10])
def test_tip_checkout_accepts_quick_amounts(api_client, base_url, quick_amount):
    walk = _create_finished_walk(api_client, base_url, f"quick_{quick_amount}_{uuid.uuid4().hex[:6]}")
    response = api_client.post(
        f"{base_url}/api/walks/{walk['id']}/tips/checkout",
        json={"quick_amount": quick_amount, "origin_url": base_url},
        timeout=30,
    )
    assert response.status_code == 200
    body = response.json()
    assert float(body["amount"]) == float(quick_amount)
    assert body["session_id"]


def test_tip_checkout_rejects_invalid_quick_amount_value(api_client, base_url):
    walk = _create_finished_walk(api_client, base_url, f"invalid_{uuid.uuid4().hex[:6]}")
    response = api_client.post(
        f"{base_url}/api/walks/{walk['id']}/tips/checkout",
        json={"quick_amount": 3},
        timeout=25,
    )
    assert response.status_code == 422


def test_walker_tip_summary_contract(api_client, base_url):
    login = requests.post(
        f"{base_url}/api/auth/login",
        json={"email": "passeador@petpasso.com", "password": "Passeador@123"},
        timeout=25,
    )
    assert login.status_code == 200
    token = login.json().get("access_token")
    assert token

    walker_session = requests.Session()
    walker_session.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/json"})
    summary_resp = walker_session.get(f"{base_url}/api/walker/tips/summary", timeout=25)
    assert summary_resp.status_code == 200

    payload = summary_resp.json()
    assert "today_total" in payload
    assert "month_total" in payload
    assert "historical_total" in payload
    assert isinstance(payload.get("recent_tips", []), list)


def test_auth_playbook_sets_http_only_cookies(base_url):
    response = requests.post(
        f"{base_url}/api/auth/login",
        json={"email": "cliente@petpasso.com", "password": "Cliente@123"},
        timeout=25,
    )
    assert response.status_code == 200
    set_cookie_header = response.headers.get("set-cookie", "").lower()
    assert "access_token=" in set_cookie_header
    assert "refresh_token=" in set_cookie_header
    assert "httponly" in set_cookie_header


def test_auth_playbook_cors_explicit_origin_with_credentials(base_url):
    origin = base_url.rstrip("/")
    response = requests.options(
        f"{base_url}/api/auth/login",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
        timeout=25,
    )
    assert response.status_code in (200, 204)
    assert response.headers.get("access-control-allow-credentials") == "true"
    assert response.headers.get("access-control-allow-origin") == origin
