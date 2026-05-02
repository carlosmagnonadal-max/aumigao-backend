from __future__ import annotations

import requests
from datetime import datetime, timedelta, timezone


# Module coverage: iteration 64 backend contracts for walkers list, walker tasks, and checklist validation.

ALLOWED_WALK_STATUSES = {
    "Agendado",
    "Indo buscar o pet",
    "Passeando agora",
    "Finalizado",
    "Cancelado",
    "Não comparecimento do cliente",
    "Não comparecimento do passeador",
    "Pendente de análise",
}


def _login(base_url: str, email: str, password: str) -> requests.Session:
    session = requests.Session()
    response = session.post(
        f"{base_url}/api/auth/login",
        json={"email": email, "password": password},
        timeout=30,
    )
    if response.status_code != 200:
        session.close()
        raise AssertionError(f"Login falhou para {email}: {response.status_code} {response.text}")
    token = (response.json() or {}).get("access_token")
    assert token, "access_token ausente no login"
    session.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/json"})
    return session


def _checklist_payload() -> dict[str, bool]:
    return {
        "checklist_confirm_water": True,
        "checklist_confirm_bowl": True,
        "checklist_confirm_bags": True,
        "checklist_confirm_first_aid": True,
    }


def test_walkers_contract_includes_kit_complete(base_url: str):
    client = _login(base_url, "cliente@petpasso.com", "Cliente@123")
    try:
        response = client.get(
            f"{base_url}/api/walkers",
            params={
                "date": (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d"),
                "duration_minutes": 30,
                "tipo_passeio": "padrao",
            },
            timeout=30,
        )
        assert response.status_code == 200, response.text
        rows = response.json()
        assert isinstance(rows, list)
        assert len(rows) > 0
        assert "kit_complete" in rows[0]
    finally:
        client.close()


def test_walker_tasks_endpoint_has_valid_status_contract(base_url: str):
    walker = _login(base_url, "walker@petpasso.com", "Walker@123")
    try:
        response = walker.get(f"{base_url}/api/walker/tasks", timeout=30)
        assert response.status_code == 200, response.text
        tasks = response.json()
        assert isinstance(tasks, list)
        for task in tasks:
            assert task.get("status") in ALLOWED_WALK_STATUSES
    finally:
        walker.close()


def test_client_can_validate_checkin_when_pending(base_url: str):
    walker = _login(base_url, "walker@petpasso.com", "Walker@123")
    client = _login(base_url, "cliente@petpasso.com", "Cliente@123")
    try:
        tasks_response = walker.get(f"{base_url}/api/walker/tasks", timeout=30)
        assert tasks_response.status_code == 200, tasks_response.text
        tasks = tasks_response.json()
        candidate = next(
            (
                item
                for item in tasks
                if item.get("status") in {"Agendado", "Indo buscar o pet"}
                and not item.get("kit_checklist_check_in_confirmed", False)
            ),
            None,
        )
        if not candidate:
            return

        walk_id = candidate["id"]
        if candidate.get("status") == "Agendado":
            checkin = walker.post(f"{base_url}/api/walks/{walk_id}/check-in", json=_checklist_payload(), timeout=30)
            assert checkin.status_code == 200, checkin.text

        validate = client.post(
            f"{base_url}/api/walks/{walk_id}/kit-checklist/check-in-validate",
            json=_checklist_payload(),
            timeout=30,
        )
        assert validate.status_code == 200, validate.text
        assert validate.json().get("kit_checklist_check_in_confirmed") is True
    finally:
        walker.close()
        client.close()