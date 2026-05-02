import uuid
from datetime import datetime, timedelta, timezone

import requests


# Module coverage: admin suporte/pendências/notificações/mensagens and critical event integrations


def _login(base_url: str, email: str, password: str) -> requests.Session:
    session = requests.Session()
    session.headers.update({"Accept": "application/json", "Content-Type": "application/json"})
    response = session.post(
        f"{base_url}/api/auth/login",
        json={"email": email, "password": password},
        timeout=20,
    )
    assert response.status_code == 200, response.text
    token = response.json().get("access_token")
    assert token
    session.headers.update({"Authorization": f"Bearer {token}"})
    return session


def _walk_payload(label: str, pet_name: str, walk_date: datetime) -> dict:
    return {
        "pet_name": pet_name,
        "client_name": "Cliente Demo",
        "walk_date": walk_date.strftime("%Y-%m-%d"),
        "walk_time": walk_date.strftime("%H:%M"),
        "duration_minutes": 30,
        "walk_type": "Individual",
        "walker_id": "walker-1",
        "pickup_street": "Rua TEST",
        "pickup_number": "100",
        "pickup_neighborhood": "Centro",
        "pickup_complement": "",
        "location_reference": f"Ref {label}",
        "pet_behavior_notes": "",
        "notes": f"TEST {label}",
    }


def _pet_payload(name: str):
    return {
        "pet_name": name,
        "behavioral_notes": "TEST behavior",
        "photo_url": "",
        "owner_name": "",
        "gets_along_with_dogs": True,
        "accepts_shared_walk": True,
        "pet_size": "Médio",
        "energy_level": "Médio",
        "pulls_leash": False,
        "dog_behavior": "Neutro",
    }


def test_support_ticket_reply_flow_and_pending_actions(base_url):
    unique = uuid.uuid4().hex[:8]
    subject = f"TEST Ticket {unique}"

    cliente = _login(base_url, "cliente@petpasso.com", "Cliente@123")
    admin = _login(base_url, "superadmin@petpasso.com", "SuperAdmin@123")

    created = cliente.post(
        f"{base_url}/api/support/tickets",
        json={"subject": subject, "message": "TEST abertura de ticket para suporte"},
        timeout=20,
    )
    assert created.status_code == 201, created.text
    ticket = created.json()
    assert ticket["subject"] == subject
    assert ticket["status"] == "aberto"

    listed = admin.get(f"{base_url}/api/support/tickets", timeout=20)
    assert listed.status_code == 200, listed.text
    listed_rows = listed.json()
    assert any(row["id"] == ticket["id"] for row in listed_rows)

    pending = admin.get(f"{base_url}/api/admin/pending-actions", timeout=20)
    assert pending.status_code == 200, pending.text
    pending_rows = pending.json()
    assert any(row["type"] == "ticket_suporte" and row["action_route"] == "/admin/suporte" for row in pending_rows)

    replied = admin.patch(
        f"{base_url}/api/support/tickets/{ticket['id']}/reply",
        json={"message": "TEST resposta admin", "status": "resolvido"},
        timeout=20,
    )
    assert replied.status_code == 200, replied.text
    replied_data = replied.json()
    assert replied_data["status"] == "resolvido"
    assert replied_data["admin_reply"] == "TEST resposta admin"

    cliente_notifications = cliente.get(f"{base_url}/api/notifications", timeout=20)
    assert cliente_notifications.status_code == 200, cliente_notifications.text
    assert any("Resposta do suporte" in row.get("title", "") for row in cliente_notifications.json())

    cliente.close()
    admin.close()


def test_admin_messages_persist_and_generate_user_notification(base_url):
    unique = uuid.uuid4().hex[:8]
    title = f"TEST Campanha {unique}"

    admin = _login(base_url, "superadmin@petpasso.com", "SuperAdmin@123")
    cliente = _login(base_url, "cliente@petpasso.com", "Cliente@123")

    created = admin.post(
        f"{base_url}/api/admin/messages",
        json={
            "title": title,
            "message": "TEST remarketing manual",
            "audience": "todos_usuarios",
        },
        timeout=20,
    )
    assert created.status_code == 201, created.text
    created_row = created.json()
    assert created_row["title"] == title
    assert created_row["sent_count"] >= 1

    listed = admin.get(f"{base_url}/api/admin/messages", timeout=20)
    assert listed.status_code == 200, listed.text
    assert any(row["id"] == created_row["id"] for row in listed.json())

    cliente_notifications = cliente.get(f"{base_url}/api/notifications", timeout=20)
    assert cliente_notifications.status_code == 200, cliente_notifications.text
    assert any(row.get("title") == title for row in cliente_notifications.json())

    admin.close()
    cliente.close()


def test_admin_notifications_mark_read_flow(base_url):
    admin = _login(base_url, "superadmin@petpasso.com", "SuperAdmin@123")

    rows_response = admin.get(f"{base_url}/api/notifications", timeout=20)
    assert rows_response.status_code == 200, rows_response.text
    rows = rows_response.json()
    assert isinstance(rows, list)
    assert len(rows) > 0

    unread = next((row for row in rows if not row.get("read", False)), rows[0])
    mark = admin.patch(f"{base_url}/api/notifications/{unread['id']}/read", timeout=20)
    assert mark.status_code == 200, mark.text
    marked = mark.json()
    assert marked["id"] == unread["id"]
    assert marked["read"] is True

    refreshed = admin.get(f"{base_url}/api/notifications", timeout=20)
    assert refreshed.status_code == 200
    refreshed_row = next(item for item in refreshed.json() if item["id"] == unread["id"])
    assert refreshed_row["read"] is True

    admin.close()


def test_walk_creation_generates_admin_notifications_and_pending_payment(base_url):
    unique = uuid.uuid4().hex[:8]
    pet_name = f"TEST Pet Walk {unique}"

    cliente = _login(base_url, "cliente@petpasso.com", "Cliente@123")
    admin = _login(base_url, "superadmin@petpasso.com", "SuperAdmin@123")

    before_notifications = admin.get(f"{base_url}/api/notifications", timeout=20)
    assert before_notifications.status_code == 200
    before_count = len(before_notifications.json())

    created_pet = cliente.post(f"{base_url}/api/pets", json=_pet_payload(pet_name), timeout=20)
    assert created_pet.status_code == 201, created_pet.text
    pet_id = created_pet.json()["id"]

    walk_dt = datetime.now(timezone.utc) + timedelta(days=2)
    walk_payload = _walk_payload(unique, pet_name, walk_dt)
    walk_payload["pet_id"] = pet_id

    created_walk = cliente.post(f"{base_url}/api/walks", json=walk_payload, timeout=20)
    assert created_walk.status_code == 201, created_walk.text
    walk = created_walk.json()
    assert walk["pet_name"] == pet_name

    after_notifications = admin.get(f"{base_url}/api/notifications", timeout=20)
    assert after_notifications.status_code == 200
    after_rows = after_notifications.json()
    assert len(after_rows) >= before_count + 2

    pending = admin.get(f"{base_url}/api/admin/pending-actions", timeout=20)
    assert pending.status_code == 200
    pending_rows = pending.json()
    assert any(row["type"] == "pagamento_pendente" for row in pending_rows)

    cliente.close()
    admin.close()
