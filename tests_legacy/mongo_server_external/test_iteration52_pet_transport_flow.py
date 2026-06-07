import os
import time
from pathlib import Path
from typing import Dict, Optional, Tuple
from datetime import datetime, timedelta, timezone

import bcrypt
import pytest
import requests
from dotenv import dotenv_values
from pymongo import MongoClient


# Module coverage: pet_transport feature flag behavior, premium estimate, transport matching/accept flow,
# transport walker filtering, and required auth playbook checks.


def _mongo_db():
    backend_env = dotenv_values("/app/backend/.env")
    mongo_url = os.environ.get("MONGO_URL") or backend_env.get("MONGO_URL")
    db_name = os.environ.get("DB_NAME") or backend_env.get("DB_NAME")
    if not mongo_url or not db_name:
        pytest.skip("MONGO_URL/DB_NAME não configurados")
    client = MongoClient(str(mongo_url).strip().strip('"'))
    db = client[str(db_name).strip().strip('"')]
    return client, db


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

    token = response.json().get("access_token")
    if not token:
        session.close()
        pytest.skip(f"Token ausente no login para {email}")

    session.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/json"})
    return session


def _transport_payload(destination_name: str, destination_reference: str = "") -> Dict[str, object]:
    suffix = str(int(time.time() * 1000))[-6:]
    future_date = (datetime.now(timezone.utc) + timedelta(days=2)).strftime("%Y-%m-%d")
    return {
        "pet_name": f"TEST_TRANSPORTE_{suffix}",
        "client_name": "Cliente Demo",
        "walk_date": future_date,
        "walk_time": "10:00",
        "duration_minutes": 45,
        "walk_type": "Individual",
        "tipo_passeio": "transporte",
        "modo_inicio_passeio": "deslocamento_premium",
        "pickup_street": "Avenida Tancredo Neves",
        "pickup_number": "1000",
        "pickup_neighborhood": "Caminho das Árvores",
        "pickup_complement": "",
        "location_reference": "Salvador Shopping",
        "local_destino_nome": destination_name,
        "local_destino_referencia": destination_reference,
        "pet_behavior_notes": "TEST comportamento ok",
        "notes": "TEST fluxo transporte",
    }


def _find_pending_request_for_matching(
    walker_session: requests.Session,
    base_url: str,
    matching_id: str,
    retries: int = 6,
) -> Optional[dict]:
    for _ in range(retries):
        response = walker_session.get(f"{base_url}/api/walker/requests", timeout=30)
        if response.status_code != 200:
            time.sleep(1)
            continue
        rows = response.json() if isinstance(response.json(), list) else []
        for row in rows:
            if str(row.get("matching_request_id") or "") == matching_id and str(row.get("status") or "") == "pending":
                return row
        time.sleep(1)
    return None


def _prepare_transport_payload_with_available_slot(
    client: requests.Session,
    base_url: str,
    destination_name: str,
    destination_reference: str,
) -> Dict[str, object]:
    payload = _transport_payload(destination_name, destination_reference)
    walkers = client.get(
        f"{base_url}/api/walkers",
        params={
            "tipo_passeio": "transporte",
            "date": payload["walk_date"],
            "duration_minutes": payload["duration_minutes"],
        },
        timeout=35,
    )
    assert walkers.status_code == 200, walkers.text
    rows = walkers.json() if isinstance(walkers.json(), list) else []
    if not rows:
        pytest.skip("Sem passeadores elegíveis em /walkers para transporte nesta janela")

    selected = rows[0]
    selected_slot = str(selected.get("selected_slot") or "").strip()
    available_slots = selected.get("available_slots") if isinstance(selected.get("available_slots"), list) else []
    slot = selected_slot or (str(available_slots[0]).strip() if available_slots else "10:00")
    payload["walk_time"] = slot
    return payload


def _clear_pending_matching_state() -> None:
    mongo_client, db = _mongo_db()
    try:
        now_iso = datetime.now(timezone.utc).isoformat()
        db.walker_requests.update_many(
            {"status": "pending"},
            {"$set": {"status": "expired", "updated_at": now_iso}},
        )
        db.matching_requests.update_many(
            {"status": "searching"},
            {"$set": {"status": "canceled", "updated_at": now_iso}},
        )
    finally:
        mongo_client.close()


@pytest.fixture()
def free_walker_capacity_scope():
    mongo_client, db = _mongo_db()
    affected: list[tuple[str, str]] = []
    try:
        walker_rows = list(
            db.users.find(
                {"email": {"$in": ["walker@petpasso.com", "passeador@petpasso.com"]}},
                {"_id": 0, "id": 1},
            )
        )
        walker_ids = [str(row.get("id") or "") for row in walker_rows if row.get("id")]
        if walker_ids:
            blocking_statuses = ["Agendado", "Indo buscar o pet", "Passeando agora", "Pendente de análise"]
            rows = list(
                db.walks.find(
                    {"walker_user_id": {"$in": walker_ids}, "status": {"$in": blocking_statuses}},
                    {"_id": 0, "id": 1, "status": 1},
                )
            )
            affected = [(str(row.get("id") or ""), str(row.get("status") or "")) for row in rows if row.get("id")]
            if affected:
                now_iso = datetime.now(timezone.utc).isoformat()
                db.walks.update_many(
                    {"id": {"$in": [row_id for row_id, _ in affected]}},
                    {"$set": {"status": "Finalizado", "updated_at": now_iso}},
                )
        yield
    finally:
        if affected:
            now_iso = datetime.now(timezone.utc).isoformat()
            for row_id, original_status in affected:
                db.walks.update_one(
                    {"id": row_id},
                    {"$set": {"status": original_status, "updated_at": now_iso}},
                )
        mongo_client.close()


@pytest.fixture()
def sessions(base_url: str):
    admin = _login(base_url, "superadmin@petpasso.com", "SuperAdmin@123")
    client = _login(base_url, "cliente@petpasso.com", "Cliente@123")
    walker_primary = _login(base_url, "walker@petpasso.com", "Walker@123")
    walker_secondary = _login(base_url, "passeador@petpasso.com", "Passeador@123")
    data = {
        "admin": admin,
        "client": client,
        "walker_primary": walker_primary,
        "walker_secondary": walker_secondary,
    }
    yield data
    for session in data.values():
        session.close()


@pytest.fixture()
def transport_flag_scope(sessions: Dict[str, requests.Session], base_url: str):
    admin = sessions["admin"]
    flags_response = admin.get(f"{base_url}/api/admin/feature-flags", timeout=30)
    assert flags_response.status_code == 200, flags_response.text
    pet_transport_flag = next(
        (item for item in flags_response.json() if item.get("feature_name") == "pet_transport"),
        None,
    )
    assert pet_transport_flag is not None

    original_active = bool(pet_transport_flag.get("is_active", False))
    original_visible = bool(pet_transport_flag.get("is_visible", False))

    yield {
        "original_active": original_active,
        "original_visible": original_visible,
    }

    admin.patch(
        f"{base_url}/api/admin/feature-flags/pet_transport",
        json={"is_active": original_active, "is_visible": original_visible},
        timeout=30,
    )


def _set_transport_flag(admin: requests.Session, base_url: str, *, active: bool, visible: bool) -> requests.Response:
    return admin.patch(
        f"{base_url}/api/admin/feature-flags/pet_transport",
        json={"is_active": active, "is_visible": visible},
        timeout=30,
    )


def _accept_matching_request(
    base_url: str,
    matching_id: str,
    walker_primary: requests.Session,
    walker_secondary: requests.Session,
) -> Tuple[str, str]:
    req = _find_pending_request_for_matching(walker_primary, base_url, matching_id)
    acting = walker_primary
    acting_name = "walker@petpasso.com"

    if not req:
        req = _find_pending_request_for_matching(walker_secondary, base_url, matching_id)
        acting = walker_secondary
        acting_name = "passeador@petpasso.com"

    if not req:
        pytest.skip("Nenhuma solicitação pendente encontrada para aceitar o matching de transporte")

    request_id = str(req.get("id") or "")
    assert request_id
    decision = acting.post(
        f"{base_url}/api/walker/requests/{request_id}/decision",
        json={"decision": "accept"},
        timeout=35,
    )
    assert decision.status_code == 200, decision.text
    return request_id, acting_name


def test_auth_bcrypt_hash_starts_with_2b_prefix():
    mongo_client, db = _mongo_db()
    try:
        row = db.users.find_one({"email": "superadmin@petpasso.com"}, {"_id": 0, "password_hash": 1})
    finally:
        mongo_client.close()

    assert row is not None
    assert str(row.get("password_hash") or "").startswith("$2b$")


def test_auth_login_sets_httponly_cookies(base_url: str):
    response = requests.post(
        f"{base_url}/api/auth/login",
        json={"email": "admin@petpasso.com", "password": "Admin@123"},
        timeout=30,
    )
    assert response.status_code == 200, response.text
    cookie_header = response.headers.get("set-cookie", "")
    assert "access_token=" in cookie_header
    assert "refresh_token=" in cookie_header
    assert "HttpOnly" in cookie_header


def test_auth_cors_preflight_explicit_origin_with_credentials(base_url: str):
    response = requests.options(
        f"{base_url}/api/auth/login",
        headers={
            "Origin": "https://petpasso-mvp.preview.emergentagent.com",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
        timeout=30,
    )
    assert response.status_code in (200, 204)
    assert response.headers.get("access-control-allow-credentials", "").lower() == "true"
    assert response.headers.get("access-control-allow-origin") == "https://petpasso-mvp.preview.emergentagent.com"


def test_auth_lockout_after_five_failed_attempts(base_url: str):
    test_ip = "203.0.113.92"
    identifier = f"{test_ip}:superadmin@petpasso.com"

    mongo_client, db = _mongo_db()
    try:
        db.login_attempts.delete_many({"identifier": identifier})
    finally:
        mongo_client.close()

    for _ in range(5):
        attempt = requests.post(
            f"{base_url}/api/auth/login",
            json={"email": "superadmin@petpasso.com", "password": "wrong-pass"},
            headers={"x-forwarded-for": test_ip},
            timeout=30,
        )
        assert attempt.status_code == 401

    blocked = requests.post(
        f"{base_url}/api/auth/login",
        json={"email": "superadmin@petpasso.com", "password": "wrong-pass"},
        headers={"x-forwarded-for": test_ip},
        timeout=30,
    )
    assert blocked.status_code == 429


def test_seed_admin_passwords_match_env_or_expected_seed_values():
    backend_env = dotenv_values("/app/backend/.env")
    expected_super_password = str(os.environ.get("SUPER_ADMIN_PASSWORD") or backend_env.get("SUPER_ADMIN_PASSWORD") or "SuperAdmin@123")
    expected_admin_password = str(os.environ.get("ADMIN_PASSWORD") or backend_env.get("ADMIN_PASSWORD") or "Admin@123")

    mongo_client, db = _mongo_db()
    try:
        super_row = db.users.find_one({"email": "superadmin@petpasso.com"}, {"_id": 0, "password_hash": 1})
        admin_row = db.users.find_one({"email": "admin@petpasso.com"}, {"_id": 0, "password_hash": 1})
    finally:
        mongo_client.close()

    assert super_row is not None
    assert admin_row is not None
    assert bcrypt.checkpw(expected_super_password.encode("utf-8"), str(super_row.get("password_hash") or "").encode("utf-8"))
    assert bcrypt.checkpw(expected_admin_password.encode("utf-8"), str(admin_row.get("password_hash") or "").encode("utf-8"))


def test_pet_transport_flag_disabled_blocks_premium_estimate(sessions, transport_flag_scope, base_url: str):
    admin = sessions["admin"]
    client = sessions["client"]

    disable = _set_transport_flag(admin, base_url, active=False, visible=False)
    assert disable.status_code == 200, disable.text

    visibility = client.get(f"{base_url}/api/feature-flags/visibility", timeout=30)
    assert visibility.status_code == 200, visibility.text
    assert visibility.json().get("flags", {}).get("pet_transport") is False

    estimate = client.post(
        f"{base_url}/api/walks/premium-estimate",
        json={
            "pickup_street": "Avenida Tancredo Neves",
            "pickup_number": "1000",
            "pickup_neighborhood": "Caminho das Árvores",
            "pickup_complement": "",
            "location_reference": "Salvador Shopping",
            "local_destino_nome": "Farol da Barra",
            "local_destino_referencia": "Barra",
            "duracao_passeio_minutos": 45,
        },
        timeout=35,
    )
    assert estimate.status_code == 400
    assert "desativado" in estimate.text.lower()


def test_pet_transport_flag_enable_allows_visibility_and_premium_estimate(sessions, transport_flag_scope, base_url: str):
    admin = sessions["admin"]
    client = sessions["client"]

    enable = _set_transport_flag(admin, base_url, active=True, visible=True)
    assert enable.status_code == 200, enable.text

    visibility = client.get(f"{base_url}/api/feature-flags/visibility", timeout=30)
    assert visibility.status_code == 200, visibility.text
    assert visibility.json().get("flags", {}).get("pet_transport") is True

    estimate = client.post(
        f"{base_url}/api/walks/premium-estimate",
        json={
            "pickup_street": "Avenida Tancredo Neves",
            "pickup_number": "1000",
            "pickup_neighborhood": "Caminho das Árvores",
            "pickup_complement": "",
            "location_reference": "Salvador Shopping",
            "local_destino_nome": "Farol da Barra",
            "local_destino_referencia": "Barra",
            "duracao_passeio_minutos": 45,
        },
        timeout=40,
    )
    assert estimate.status_code == 200, estimate.text
    payload = estimate.json()
    assert payload["tipoPasseio"] == "transporte"
    assert isinstance(payload.get("distanciaKm"), (int, float))
    assert int(payload.get("trackingIntervalSegundos") or 0) == 15


def test_feature_flag_lacks_test_user_scoping_for_pet_transport(sessions, base_url: str):
    admin = sessions["admin"]
    settings = admin.get(f"{base_url}/api/admin/pet-transport/settings", timeout=30)
    assert settings.status_code == 200, settings.text
    payload = settings.json()
    assert "pet_transport_enabled_for" in payload, "Ausente suporte de escopo por usuários de teste (pet_transport_enabled_for)"


def test_walker_filter_for_transport_returns_only_eligible_profiles(sessions, transport_flag_scope, base_url: str):
    admin = sessions["admin"]
    client = sessions["client"]
    enable = _set_transport_flag(admin, base_url, active=True, visible=True)
    assert enable.status_code == 200, enable.text

    walkers = client.get(
        f"{base_url}/api/walkers",
        params={"tipo_passeio": "transporte", "date": "2026-01-21", "duration_minutes": 45, "preferred_time": "10:00"},
        timeout=35,
    )
    assert walkers.status_code == 200, walkers.text
    rows = walkers.json() if isinstance(walkers.json(), list) else []
    assert rows, "Nenhum passeador retornado para transporte"
    for walker in rows:
        assert bool(walker.get("possuiVeiculo")) is True
        assert bool(walker.get("aceitaDeslocamentoPremium")) is True
        assert bool(walker.get("ativoParaTransportePremium")) is True


def test_matching_request_transport_payload_persisted_in_db(sessions, transport_flag_scope, free_walker_capacity_scope, base_url: str):
    admin = sessions["admin"]
    client = sessions["client"]
    enable = _set_transport_flag(admin, base_url, active=True, visible=True)
    assert enable.status_code == 200, enable.text
    _clear_pending_matching_state()

    payload = _prepare_transport_payload_with_available_slot(client, base_url, "Farol da Barra", "Barra")
    created = client.post(f"{base_url}/api/walks/matching-request", json=payload, timeout=45)
    assert created.status_code == 201, created.text
    matching_id = created.json()["id"]

    mongo_client, db = _mongo_db()
    try:
        row = db.matching_requests.find_one({"id": matching_id}, {"_id": 0})
    finally:
        mongo_client.close()

    assert row is not None
    assert row.get("tipo_passeio") == "transporte"
    assert row.get("modo_inicio_passeio") == "deslocamento_premium"
    assert str(row.get("local_destino_nome") or "") == "Farol da Barra"
    assert str(row.get("local_destino_referencia") or "") == "Barra"


def test_matching_accept_creates_transport_walk_with_tracking_and_total_time(sessions, transport_flag_scope, free_walker_capacity_scope, base_url: str):
    admin = sessions["admin"]
    client = sessions["client"]
    walker_primary = sessions["walker_primary"]
    walker_secondary = sessions["walker_secondary"]

    enable = _set_transport_flag(admin, base_url, active=True, visible=True)
    assert enable.status_code == 200, enable.text
    _clear_pending_matching_state()

    payload = _prepare_transport_payload_with_available_slot(client, base_url, "Itaigara", "Salvador")
    created = client.post(f"{base_url}/api/walks/matching-request", json=payload, timeout=45)
    assert created.status_code == 201, created.text
    matching_id = created.json()["id"]

    request_id, _ = _accept_matching_request(base_url, matching_id, walker_primary, walker_secondary)
    walk_id = f"walker-request-{request_id}"

    walk_response = admin.get(f"{base_url}/api/walks/{walk_id}", timeout=35)
    assert walk_response.status_code == 200, walk_response.text
    walk = walk_response.json()
    assert walk.get("tipoPasseio") == "transporte"
    assert int(walk.get("trackingIntervalSegundos") or 0) == 15
    assert bool(walk.get("rastreamentoReforcado")) is True

    passeio_min = int(walk.get("tempoPasseioMinutos") or 0)
    desloc_min = int(walk.get("tempoDeslocamentoMinutos") or 0)
    total_min = int(walk.get("tempoTotalMinutos") or 0)
    assert total_min == passeio_min + desloc_min


def test_matching_accept_far_distance_sets_manual_review_pending_status(sessions, transport_flag_scope, free_walker_capacity_scope, base_url: str):
    admin = sessions["admin"]
    client = sessions["client"]
    walker_primary = sessions["walker_primary"]
    walker_secondary = sessions["walker_secondary"]

    enable = _set_transport_flag(admin, base_url, active=True, visible=True)
    assert enable.status_code == 200, enable.text
    _clear_pending_matching_state()

    payload = _prepare_transport_payload_with_available_slot(client, base_url, "Farol da Barra", "Barra")
    created = client.post(f"{base_url}/api/walks/matching-request", json=payload, timeout=45)
    assert created.status_code == 201, created.text
    matching_id = created.json()["id"]

    request_id, _ = _accept_matching_request(base_url, matching_id, walker_primary, walker_secondary)
    walk_id = f"walker-request-{request_id}"

    walk_response = admin.get(f"{base_url}/api/walks/{walk_id}", timeout=35)
    assert walk_response.status_code == 200, walk_response.text
    walk = walk_response.json()

    assert walk.get("tipoPasseio") == "transporte"
    assert walk.get("status") == "Pendente de análise"
    assert bool(walk.get("precisaAnaliseManualDeslocamento")) is True
    assert walk.get("statusAnaliseDeslocamento") == "aguardando_analise"
