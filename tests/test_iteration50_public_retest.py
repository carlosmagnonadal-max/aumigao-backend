"""Iteration 50 retest - public endpoint auth CORS + pet-routine quick regression."""

from __future__ import annotations

from pathlib import Path
from typing import Tuple

import pytest
import requests
from dotenv import dotenv_values


# Public URL and auth helpers for retesting what users access in preview.
#
# Estes testes sao de INTEGRACAO contra um servidor publico ja no ar: leem a URL
# de /app/frontend/.env (caminho do container Linux do ambiente de preview) e
# fazem requests HTTP reais. Fora desse ambiente — em qualquer maquina de dev,
# Windows incluso — o arquivo nao existe e os 4 testes falhavam sempre com
# RuntimeError, poluindo a suite com vermelho permanente que nao indica
# regressao alguma. O skipif abaixo torna a indisponibilidade explicita
# (skipped, nao failed) sem perder a cobertura onde o ambiente existe.
def _resolve_public_base_url() -> str | None:
    """URL publica configurada, ou None quando o ambiente de preview nao existe."""
    env_file = Path("/app/frontend/.env")
    values = dotenv_values(str(env_file)) if env_file.exists() else {}
    return str(values.get("EXPO_PUBLIC_BACKEND_URL") or "").strip().rstrip("/") or None


pytestmark = pytest.mark.skipif(
    _resolve_public_base_url() is None,
    reason="EXPO_PUBLIC_BACKEND_URL nao configurada em /app/frontend/.env "
           "(ambiente de preview ausente) — teste de integracao contra servidor publico",
)


def _public_base_url() -> str:
    resolved = _resolve_public_base_url()
    if not resolved:
        raise RuntimeError("EXPO_PUBLIC_BACKEND_URL não configurada em frontend/.env")
    return resolved


def _login(email: str, password: str) -> Tuple[requests.Session, requests.Response]:
    session = requests.Session()
    response = session.post(
        f"{_public_base_url()}/api/auth/login",
        json={"email": email, "password": password},
        timeout=30,
    )
    if response.ok:
        token = response.json().get("access_token")
        if token:
            session.headers.update({"Authorization": f"Bearer {token}"})
    return session, response


# Auth/CORS bug retest for strict credentialed CORS requirements on public endpoint.
def test_public_preflight_login_has_explicit_origin_and_credentials_true():
    response = requests.options(
        f"{_public_base_url()}/api/auth/login",
        headers={
            "Origin": "https://petpasso-mvp.preview.emergentagent.com",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
        timeout=20,
    )

    assert response.status_code in (200, 204)
    assert response.headers.get("Access-Control-Allow-Credentials", "").lower() == "true"
    assert response.headers.get("Access-Control-Allow-Origin") == "https://petpasso-mvp.preview.emergentagent.com"


def test_public_login_sets_httponly_cookies():
    session, login = _login("admin@petpasso.com", "Admin@123")
    try:
        assert login.status_code == 200, login.text
        cookie_header = (login.headers.get("set-cookie") or "").lower()
        assert "access_token=" in cookie_header
        assert "refresh_token=" in cookie_header
        assert "httponly" in cookie_header
    finally:
        session.close()


# Public pet-routine regression for CRUD/list/dashboard/recalc access paths.
def test_public_pet_routine_dashboard_and_list_for_cliente():
    session, login = _login("cliente@petpasso.com", "Cliente@123")
    if login.status_code != 200:
        session.close()
        pytest.skip(f"Login cliente indisponível no endpoint público: {login.status_code}")

    try:
        routines = session.get(f"{_public_base_url()}/api/pet-routines", timeout=30)
        assert routines.status_code == 200, routines.text
        assert isinstance(routines.json(), list)

        dashboard = session.get(f"{_public_base_url()}/api/pet-routine/dashboard", timeout=30)
        assert dashboard.status_code == 200, dashboard.text
        payload = dashboard.json()
        assert "progress" in payload
        assert "routine" in payload
    finally:
        session.close()


def test_public_admin_recalculate_pet_routine_endpoint():
    client_session, client_login = _login("cliente@petpasso.com", "Cliente@123")
    admin_session, admin_login = _login("superadmin@petpasso.com", "SuperAdmin@123")

    if client_login.status_code != 200 or admin_login.status_code != 200:
        client_session.close()
        admin_session.close()
        pytest.skip("Login cliente/admin indisponível no endpoint público")

    try:
        me = client_session.get(f"{_public_base_url()}/api/auth/me", timeout=30)
        assert me.status_code == 200, me.text
        user_id = me.json().get("id")

        routines = client_session.get(f"{_public_base_url()}/api/pet-routines", timeout=30)
        assert routines.status_code == 200, routines.text
        items = routines.json() if isinstance(routines.json(), list) else []
        if not items:
            pytest.skip("Cliente sem rotina para recálculo no endpoint público")

        pet_id = items[0].get("pet_id")
        if not user_id or not pet_id:
            pytest.skip("Dados insuficientes para recálculo no endpoint público")

        recalc = admin_session.post(
            f"{_public_base_url()}/api/admin/pet-routine/recalculate",
            json={"user_id": user_id, "pet_id": pet_id},
            timeout=35,
        )
        assert recalc.status_code == 200, recalc.text
        recalc_payload = recalc.json()
        assert int(recalc_payload.get("processed_users") or 0) >= 1
    finally:
        client_session.close()
        admin_session.close()
