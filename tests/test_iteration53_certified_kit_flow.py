import time
from datetime import datetime, timedelta

import pytest
import requests


# Module coverage: certified walker kit levels/audit, mandatory checklists, handover blocking, and kit issue report safeguards.


def _login(base_url: str, email: str, password: str) -> requests.Session:
    session = requests.Session()
    response = session.post(
        f"{base_url}/api/auth/login",
        json={"email": email, "password": password},
        timeout=30,
    )
    if response.status_code != 200:
        session.close()
        pytest.skip(f"Login indisponível para {email}: {response.status_code}")

    token = (response.json() or {}).get("access_token")
    if not token:
        session.close()
        pytest.skip(f"Token ausente no login para {email}")

    session.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/json"})
    return session


def _checklist_payload(valid: bool = True):
    if valid:
        return {
            "checklist_confirm_water": True,
            "checklist_confirm_bowl": True,
            "checklist_confirm_bags": True,
        }
    return {
        "checklist_confirm_water": True,
        "checklist_confirm_bowl": False,
        "checklist_confirm_bags": True,
    }


def _get_first_walk_id(session: requests.Session, base_url: str, allowed_statuses: tuple[str, ...]) -> str:
    response = session.get(f"{base_url}/api/walker/tasks", timeout=30)
    assert response.status_code == 200, response.text
    rows = response.json() if isinstance(response.json(), list) else []
    for row in rows:
        if str(row.get("status") or "") in allowed_statuses:
            return str(row.get("id") or "")
    pytest.skip(f"Sem passeio disponível nos status {allowed_statuses}")


@pytest.fixture()
def role_sessions(base_url: str):
    walker = _login(base_url, "passeador@petpasso.com", "Passeador@123")
    admin = _login(base_url, "superadmin@petpasso.com", "SuperAdmin@123")
    client = _login(base_url, "cliente@petpasso.com", "Cliente@123")
    data = {"walker": walker, "admin": admin, "client": client}
    yield data
    for session in data.values():
        session.close()


def test_get_patch_certified_kit_reflects_level_and_labels(base_url: str, role_sessions):
    walker = role_sessions["walker"]
    original = walker.get(f"{base_url}/api/walker/certified-kit", timeout=30)
    assert original.status_code == 200, original.text
    original_data = original.json()

    try:
        level1_payload = {
            "has_water": True,
            "has_bowl": True,
            "has_bags": True,
            "has_first_aid": False,
            "has_towel": False,
            "has_extra_leash": False,
            "has_premium_items": False,
        }
        level1 = walker.patch(f"{base_url}/api/walker/certified-kit", json=level1_payload, timeout=30)
        assert level1.status_code == 200, level1.text
        level1_data = level1.json()
        assert level1_data["kit_basic_complete"] is True
        assert level1_data["kit_level"] == 1
        assert "Kit Básico Completo" in level1_data["kit_labels"]

        level3_payload = {
            "has_first_aid": True,
            "has_towel": True,
            "has_extra_leash": True,
            "has_premium_items": True,
        }
        level3 = walker.patch(f"{base_url}/api/walker/certified-kit", json=level3_payload, timeout=30)
        assert level3.status_code == 200, level3.text
        level3_data = level3.json()
        assert level3_data["kit_essential_complete"] is True
        assert level3_data["kit_premium"] is True
        assert level3_data["kit_level"] >= 3
        assert "Passeador Premium" in level3_data["kit_labels"]
    finally:
        restore_payload = {
            key: original_data.get(key)
            for key in [
                "has_water",
                "has_bowl",
                "has_bags",
                "has_first_aid",
                "has_towel",
                "has_extra_leash",
                "has_premium_items",
            ]
        }
        walker.patch(f"{base_url}/api/walker/certified-kit", json=restore_payload, timeout=30)


def test_check_in_blocks_without_basic_confirmations(base_url: str, role_sessions):
    walker = role_sessions["walker"]
    walk_id = _get_first_walk_id(walker, base_url, ("Agendado",))

    response = walker.post(
        f"{base_url}/api/walks/{walk_id}/check-in",
        json=_checklist_payload(valid=False),
        timeout=30,
    )
    assert response.status_code == 400
    assert "Checklist obrigatório" in response.text


def test_basic_kit_required_to_check_in(base_url: str, role_sessions):
    walker = role_sessions["walker"]
    walk_id = _get_first_walk_id(walker, base_url, ("Agendado",))

    before = walker.get(f"{base_url}/api/walker/certified-kit", timeout=30)
    assert before.status_code == 200, before.text
    original = before.json()

    try:
        degrade = walker.patch(
            f"{base_url}/api/walker/certified-kit",
            json={"has_water": True, "has_bowl": True, "has_bags": False},
            timeout=30,
        )
        assert degrade.status_code == 200, degrade.text
        blocked = walker.post(
            f"{base_url}/api/walks/{walk_id}/check-in",
            json=_checklist_payload(valid=True),
            timeout=30,
        )
        assert blocked.status_code == 400
        assert "Nível básico do kit é obrigatório" in blocked.text
    finally:
        walker.patch(
            f"{base_url}/api/walker/certified-kit",
            json={
                "has_water": original.get("has_water"),
                "has_bowl": original.get("has_bowl"),
                "has_bags": original.get("has_bags"),
            },
            timeout=30,
        )


def test_start_checklist_required_before_confirm_handover(base_url: str, role_sessions):
    walker = role_sessions["walker"]
    admin = role_sessions["admin"]
    walk_id = _get_first_walk_id(walker, base_url, ("Agendado",))

    check_in = walker.post(
        f"{base_url}/api/walks/{walk_id}/check-in",
        json=_checklist_payload(valid=True),
        timeout=30,
    )
    if check_in.status_code == 200:
        pass
    elif check_in.status_code in (400, 409) and "já" in check_in.text.lower():
        pass
    else:
        assert check_in.status_code == 200, check_in.text

    handover_blocked = admin.post(f"{base_url}/api/walks/{walk_id}/confirm-handover", timeout=30)
    assert handover_blocked.status_code == 400
    assert "Checklist obrigatório do passeador pendente" in handover_blocked.text

    start_invalid = walker.post(
        f"{base_url}/api/walks/{walk_id}/kit-checklist/start",
        json=_checklist_payload(valid=False),
        timeout=30,
    )
    assert start_invalid.status_code == 400

    start_valid = walker.post(
        f"{base_url}/api/walks/{walk_id}/kit-checklist/start",
        json=_checklist_payload(valid=True),
        timeout=30,
    )
    assert start_valid.status_code == 200, start_valid.text
    assert start_valid.json().get("kit_checklist_start_confirmed") is True

    handover_ok = admin.post(f"{base_url}/api/walks/{walk_id}/confirm-handover", timeout=30)
    assert handover_ok.status_code == 200, handover_ok.text
    assert handover_ok.json().get("status") in ("Passeando agora", "Finalizado")


def test_kit_issue_report_requires_confirmation(base_url: str, role_sessions):
    client = role_sessions["client"]
    walks = client.get(f"{base_url}/api/walks", timeout=30)
    assert walks.status_code == 200, walks.text
    rows = walks.json() if isinstance(walks.json(), list) else []
    target = next((row for row in rows if str(row.get("status") or "") != "Cancelado"), None)
    if not target:
        pytest.skip("Cliente sem passeio elegível para denúncia de kit")

    walk_id = str(target.get("id") or "")
    response = client.post(
        f"{base_url}/api/walks/{walk_id}/kit-issue-report",
        json={
            "confirm_report": False,
            "missing_items": ["has_water"],
            "note": "TEST sem confirmação",
        },
        timeout=30,
    )
    assert response.status_code == 400
    assert "Confirme o relato" in response.text


def test_admin_can_audit_walker_kit(base_url: str, role_sessions):
    walker = role_sessions["walker"]
    admin = role_sessions["admin"]

    me = walker.get(f"{base_url}/api/auth/me", timeout=30)
    assert me.status_code == 200, me.text
    walker_user_id = me.json().get("id")
    assert walker_user_id

    audit = admin.patch(
        f"{base_url}/api/admin/walker-kit/{walker_user_id}/audit",
        json={"kit_audit_status": "aprovado", "kit_audit_note": "TEST auditoria it53"},
        timeout=30,
    )
    assert audit.status_code == 200, audit.text
    audit_data = audit.json()
    assert audit_data.get("kit_audit_status") == "aprovado"
    assert "TEST auditoria it53" in str(audit_data.get("kit_audit_note") or "")


def test_walker_kit_photo_upload_base64_persists(base_url: str, role_sessions):
    walker = role_sessions["walker"]
    original = walker.get(f"{base_url}/api/walker/certified-kit", timeout=30)
    assert original.status_code == 200, original.text
    original_photos = list((original.json() or {}).get("kit_photos_base64") or [])

    fake_png_data_uri = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR4nGNgYAAAAAMAASsJTYQAAAAASUVORK5CYII="

    try:
        update = walker.patch(
            f"{base_url}/api/walker/certified-kit",
            json={"kit_photos_base64": [fake_png_data_uri]},
            timeout=30,
        )
        assert update.status_code == 200, update.text
        payload = update.json()
        photos = payload.get("kit_photos_base64") or []
        assert len(photos) == 1
        assert str(photos[0]).startswith("data:image/png;base64,")
    finally:
        walker.patch(
            f"{base_url}/api/walker/certified-kit",
            json={"kit_photos_base64": original_photos},
            timeout=30,
        )


def test_client_list_walkers_exposes_kit_levels_and_labels(base_url: str, role_sessions):
    client = role_sessions["client"]
    query_date = datetime.utcnow().strftime("%Y-%m-%d")
    response = client.get(
        f"{base_url}/api/walkers",
        params={"date": query_date, "duration_minutes": 30, "tipo_passeio": "padrao"},
        timeout=30,
    )
    assert response.status_code == 200, response.text

    walkers = response.json() if isinstance(response.json(), list) else []
    assert len(walkers) > 0

    first = walkers[0]
    assert isinstance(first.get("kit_level"), int)
    assert isinstance(first.get("kit_labels"), list)
    if first.get("kit_level", 0) >= 1:
        assert "Kit Básico Completo" in first.get("kit_labels", [])


def test_login_scope_user_account(base_url: str):
    response = requests.post(
        f"{base_url}/api/auth/login",
        json={"email": "scope.user@gmail.com", "password": "Cliente@123"},
        timeout=30,
    )
    assert response.status_code == 200, response.text
    assert (response.json() or {}).get("access_token")
