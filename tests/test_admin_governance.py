import requests
import uuid


def _login(base_url: str, email: str, password: str) -> str:
    response = requests.post(
        f"{base_url}/api/auth/login",
        json={"email": email, "password": password},
        timeout=20,
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def _permissions_map(**enabled):
    base = {
        "dashboard": False,
        "clientes": False,
        "passeios": False,
        "pagamentos": False,
        "passeadores": False,
        "planos": False,
        "suporte": False,
        "configuracoes": False,
        "juridico": False,
        "administradores": False,
    }
    for key, value in enabled.items():
        if key in base:
            base[key] = bool(value)
    return base


def test_public_register_cannot_create_admin(base_url):
    response = requests.post(
        f"{base_url}/api/auth/register",
        json={
            "full_name": "Teste Bloqueio Admin",
            "email": "teste-bloqueio-admin@petpasso.com",
            "password": "Senha@123",
            "role": "admin",
            "accepted_terms": True,
            "accepted_privacy": True,
            "accepted_lgpd": True,
        },
        timeout=20,
    )
    assert response.status_code == 422


def test_super_admin_can_create_admin_account(base_url):
    super_token = _login(base_url, "superadmin@petpasso.com", "SuperAdmin@123")
    unique_email = f"admin-criado-super-{uuid.uuid4().hex[:8]}@petpasso.com"
    payload = {
        "full_name": "Admin Criado por Super",
        "email": unique_email,
        "password": "Admin@1234",
        "role": "admin",
        "isActive": True,
        "permissions": _permissions_map(dashboard=True, clientes=True, passeios=True, passeadores=True, suporte=True),
    }
    response = requests.post(
        f"{base_url}/api/admin/administrators",
        json=payload,
        headers={"Authorization": f"Bearer {super_token}"},
        timeout=20,
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["role"] == "admin"
    assert body["permissions"]["dashboard"] is True
    assert body["permissions"]["pagamentos"] is False


def test_admin_with_permission_can_create_admin_without_escalation(base_url):
    super_token = _login(base_url, "superadmin@petpasso.com", "SuperAdmin@123")
    admin_token = _login(base_url, "admin@petpasso.com", "Admin@123")

    admin_rows = requests.get(
        f"{base_url}/api/admin/administrators",
        headers={"Authorization": f"Bearer {super_token}"},
        timeout=20,
    )
    assert admin_rows.status_code == 200, admin_rows.text
    admin_id = next(item["id"] for item in admin_rows.json() if item["email"] == "admin@petpasso.com")

    grant_create = requests.patch(
        f"{base_url}/api/admin/administrators/{admin_id}",
        json={
            "permissions": _permissions_map(
                dashboard=True,
                clientes=True,
                passeios=True,
                passeadores=True,
                suporte=True,
                administradores=True,
            )
        },
        headers={"Authorization": f"Bearer {super_token}"},
        timeout=20,
    )
    assert grant_create.status_code == 200, grant_create.text

    create_ok = requests.post(
        f"{base_url}/api/admin/administrators",
        json={
            "full_name": "Admin Limitado",
            "email": f"admin-limitado-{uuid.uuid4().hex[:8]}@petpasso.com",
            "password": "Admin@1234",
            "role": "admin",
            "isActive": True,
            "permissions": _permissions_map(dashboard=True, clientes=True),
        },
        headers={"Authorization": f"Bearer {admin_token}"},
        timeout=20,
    )
    assert create_ok.status_code == 201, create_ok.text

    create_escalated = requests.post(
        f"{base_url}/api/admin/administrators",
        json={
            "full_name": "Tentativa Escalada",
            "email": f"tentativa-escalada-{uuid.uuid4().hex[:8]}@petpasso.com",
            "password": "Admin@1234",
            "role": "admin",
            "isActive": True,
            "permissions": _permissions_map(dashboard=True, pagamentos=True),
        },
        headers={"Authorization": f"Bearer {admin_token}"},
        timeout=20,
    )
    assert create_escalated.status_code == 403


def test_admin_cannot_create_super_admin(base_url):
    super_token = _login(base_url, "superadmin@petpasso.com", "SuperAdmin@123")
    admin_token = _login(base_url, "admin@petpasso.com", "Admin@123")

    admin_rows = requests.get(
        f"{base_url}/api/admin/administrators",
        headers={"Authorization": f"Bearer {super_token}"},
        timeout=20,
    )
    admin_id = next(item["id"] for item in admin_rows.json() if item["email"] == "admin@petpasso.com")
    requests.patch(
        f"{base_url}/api/admin/administrators/{admin_id}",
        json={"permissions": _permissions_map(dashboard=True, administradores=True)},
        headers={"Authorization": f"Bearer {super_token}"},
        timeout=20,
    )

    create_super = requests.post(
        f"{base_url}/api/admin/administrators",
        json={
            "full_name": "Tentativa Super",
            "email": f"tentativa-super-{uuid.uuid4().hex[:8]}@petpasso.com",
            "password": "Admin@1234",
            "role": "super_admin",
            "isActive": True,
            "permissions": _permissions_map(dashboard=True),
        },
        headers={"Authorization": f"Bearer {admin_token}"},
        timeout=20,
    )
    assert create_super.status_code == 403


def test_module_permission_enforced_and_logs_available(base_url):
    super_token = _login(base_url, "superadmin@petpasso.com", "SuperAdmin@123")
    admin_token = _login(base_url, "admin@petpasso.com", "Admin@123")

    admin_rows = requests.get(
        f"{base_url}/api/admin/administrators",
        headers={"Authorization": f"Bearer {super_token}"},
        timeout=20,
    )
    admin_id = next(item["id"] for item in admin_rows.json() if item["email"] == "admin@petpasso.com")

    narrow_permissions = requests.patch(
        f"{base_url}/api/admin/administrators/{admin_id}",
        json={"permissions": _permissions_map(dashboard=True)},
        headers={"Authorization": f"Bearer {super_token}"},
        timeout=20,
    )
    assert narrow_permissions.status_code == 200, narrow_permissions.text

    blocked_clients = requests.get(
        f"{base_url}/api/admin/clients",
        headers={"Authorization": f"Bearer {admin_token}"},
        timeout=20,
    )
    assert blocked_clients.status_code == 403

    allowed_dashboard = requests.get(
        f"{base_url}/api/admin/dashboard",
        headers={"Authorization": f"Bearer {admin_token}"},
        timeout=20,
    )
    assert allowed_dashboard.status_code == 200, allowed_dashboard.text

    logs = requests.get(
        f"{base_url}/api/admin/administrators/logs",
        headers={"Authorization": f"Bearer {super_token}"},
        timeout=20,
    )
    assert logs.status_code == 200, logs.text
    assert isinstance(logs.json(), list)


def test_permissions_are_boolean_object_shape(base_url):
    super_token = _login(base_url, "superadmin@petpasso.com", "SuperAdmin@123")
    response = requests.get(
        f"{base_url}/api/admin/administrators",
        headers={"Authorization": f"Bearer {super_token}"},
        timeout=20,
    )
    assert response.status_code == 200, response.text
    rows = response.json()
    assert rows, "Expected at least one admin account"

    permissions = rows[0]["permissions"]
    assert isinstance(permissions, dict)
    assert set(permissions.keys()) == {
        "dashboard",
        "clientes",
        "passeios",
        "pagamentos",
        "passeadores",
        "planos",
        "suporte",
        "configuracoes",
        "juridico",
        "administradores",
    }
    assert all(isinstance(value, bool) for value in permissions.values())


def test_admin_without_administradores_permission_cannot_create_admin(base_url):
    super_token = _login(base_url, "superadmin@petpasso.com", "SuperAdmin@123")
    admin_token = _login(base_url, "admin@petpasso.com", "Admin@123")

    admin_rows = requests.get(
        f"{base_url}/api/admin/administrators",
        headers={"Authorization": f"Bearer {super_token}"},
        timeout=20,
    )
    assert admin_rows.status_code == 200, admin_rows.text
    admin_id = next(item["id"] for item in admin_rows.json() if item["email"] == "admin@petpasso.com")

    revoke_permission = requests.patch(
        f"{base_url}/api/admin/administrators/{admin_id}",
        json={"permissions": _permissions_map(dashboard=True, clientes=True)},
        headers={"Authorization": f"Bearer {super_token}"},
        timeout=20,
    )
    assert revoke_permission.status_code == 200, revoke_permission.text

    create_attempt = requests.post(
        f"{base_url}/api/admin/administrators",
        json={
            "full_name": "Admin Bloqueado",
            "email": f"admin-bloqueado-{uuid.uuid4().hex[:8]}@petpasso.com",
            "password": "Admin@1234",
            "role": "admin",
            "isActive": True,
            "permissions": _permissions_map(dashboard=True),
        },
        headers={"Authorization": f"Bearer {admin_token}"},
        timeout=20,
    )
    assert create_attempt.status_code == 403


def test_inactive_admin_cannot_login(base_url):
    super_token = _login(base_url, "superadmin@petpasso.com", "SuperAdmin@123")
    inactive_email = f"admin-inativo-{uuid.uuid4().hex[:8]}@petpasso.com"
    initial_password = "Admin@1234"

    create_response = requests.post(
        f"{base_url}/api/admin/administrators",
        json={
            "full_name": "Admin Inativo",
            "email": inactive_email,
            "password": initial_password,
            "role": "admin",
            "isActive": False,
            "permissions": _permissions_map(dashboard=True),
        },
        headers={"Authorization": f"Bearer {super_token}"},
        timeout=20,
    )
    assert create_response.status_code == 201, create_response.text

    login_blocked = requests.post(
        f"{base_url}/api/auth/login",
        json={"email": inactive_email, "password": initial_password},
        timeout=20,
    )
    assert login_blocked.status_code == 403
    assert "Conta inativa" in login_blocked.text