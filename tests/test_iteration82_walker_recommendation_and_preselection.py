from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests
from dotenv import dotenv_values
from pymongo import MongoClient


# Módulos cobertos: ranking inteligente/preseleção do /api/walkers + checks essenciais do auth playbook.

CLIENT_CREDS = {"email": "cliente@petpasso.com", "password": "Cliente@123"}
ADMIN_CREDS = {"email": "admin@petpasso.com", "password": "Admin@123"}


def _mongo_db():
    values = dotenv_values("/app/backend/.env")
    mongo_url = str(os.environ.get("MONGO_URL") or values.get("MONGO_URL") or "").strip().strip('"')
    db_name = str(os.environ.get("DB_NAME") or values.get("DB_NAME") or "").strip().strip('"')
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
    token = (response.json() or {}).get("access_token")
    assert isinstance(token, str) and token
    session.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/json"})
    return session


def _walkers_query_params() -> dict:
    return {
        "date": (datetime.now(timezone.utc) + timedelta(days=2)).strftime("%Y-%m-%d"),
        "duration_minutes": 30,
        "preferred_time": "09:00",
        "neighborhood": "Centro",
        "tipo_passeio": "padrao",
        "selected_pets_count": 1,
    }


def test_walkers_returns_business_priority_components_and_sorted_top(base_url: str):
    client = _login(base_url, CLIENT_CREDS["email"], CLIENT_CREDS["password"])
    try:
        response = client.get(f"{base_url}/api/walkers", params=_walkers_query_params(), timeout=35)
        assert response.status_code == 200, response.text
        rows = response.json() if isinstance(response.json(), list) else []
        assert rows, "Sem passeadores para validar ranking"

        top = rows[0]
        for field in [
            "business_priority_score",
            "conversion_priority_score",
            "reliability_priority_score",
            "margin_priority_score",
            "calendar_priority_score",
            "ranking_score_final",
        ]:
            assert field in top
            assert isinstance(top.get(field), (int, float))

        eligible_rows = [row for row in rows if row.get("is_eligible_for_matching")]
        if eligible_rows:
            top_eligible = eligible_rows[0]
            max_business = max(float(row.get("business_priority_score") or 0.0) for row in eligible_rows)
            assert float(top_eligible.get("business_priority_score") or 0.0) == pytest.approx(max_business, abs=1e-2)
    finally:
        client.close()


def test_walkers_has_contextual_recommended_labels_on_top_candidates(base_url: str):
    client = _login(base_url, CLIENT_CREDS["email"], CLIENT_CREDS["password"])
    try:
        response = client.get(f"{base_url}/api/walkers", params=_walkers_query_params(), timeout=35)
        assert response.status_code == 200, response.text
        rows = response.json() if isinstance(response.json(), list) else []
        assert rows

        top_candidates = rows[:3]
        labels = [str(row.get("recommended_label") or "") for row in top_candidates]

        assert any(label in {"Mais escolhido", "Mais confiável", "Recomendado para você", "Seu passeador preferido"} for label in labels)
        if len(rows) >= 2:
            assert any(label == "Mais escolhido" for label in labels)
            assert any(label == "Mais confiável" for label in labels)
    finally:
        client.close()


def test_walkers_returns_preselection_hints_for_top_match(base_url: str):
    client = _login(base_url, CLIENT_CREDS["email"], CLIENT_CREDS["password"])
    try:
        response = client.get(f"{base_url}/api/walkers", params=_walkers_query_params(), timeout=35)
        assert response.status_code == 200, response.text
        rows = response.json() if isinstance(response.json(), list) else []
        assert rows

        top = rows[0]
        assert "available_slots" in top
        assert isinstance(top.get("available_slots"), list)
        assert "selected_slot" in top
        assert "is_top_match" in top
        assert "recommended_label" in top
        assert isinstance(top.get("is_top_match"), bool)

        slots = top.get("available_slots") or []
        selected_slot = str(top.get("selected_slot") or "")
        if slots and selected_slot:
            assert selected_slot in slots
    finally:
        client.close()


def test_walkers_value_context_labels_include_trust_copy_when_applicable(base_url: str):
    client = _login(base_url, CLIENT_CREDS["email"], CLIENT_CREDS["password"])
    try:
        response = client.get(f"{base_url}/api/walkers", params=_walkers_query_params(), timeout=35)
        assert response.status_code == 200, response.text
        rows = response.json() if isinstance(response.json(), list) else []
        assert rows

        trust_values = {
            "Passeador verificado",
            "Alta taxa de conclusão",
            "Sem cancelamentos recentes",
            "Agenda organizada",
        }
        top_candidates = rows[:3]
        top_contexts = [label for row in top_candidates for label in (row.get("value_context_labels") or [])]

        assert any(label in trust_values for label in top_contexts), "Top candidatos sem trust/value context"
    finally:
        client.close()


def test_auth_playbook_login_sets_http_only_cookies(base_url: str):
    response = requests.post(
        f"{base_url}/api/auth/login",
        json=CLIENT_CREDS,
        timeout=30,
    )
    assert response.status_code == 200, response.text
    cookie_header = "\n".join(response.raw.headers.get_all("Set-Cookie") if response.raw and response.raw.headers else [response.headers.get("set-cookie", "")])
    assert "access_token=" in cookie_header
    assert "refresh_token=" in cookie_header
    assert "HttpOnly" in cookie_header


def test_auth_playbook_cors_allows_credentials_with_explicit_origin(base_url: str):
    origin = "https://petpasso-mvp.preview.emergentagent.com"
    preflight = requests.options(
        f"{base_url}/api/auth/login",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
        timeout=30,
    )
    assert preflight.status_code in {200, 204}
    assert preflight.headers.get("access-control-allow-origin") == origin
    assert preflight.headers.get("access-control-allow-credentials") == "true"


def test_auth_playbook_bruteforce_lockout_after_five_fails(base_url: str):
    unique_ip = f"198.51.100.{int(uuid.uuid4().hex[:2], 16)}"
    statuses = []
    for _ in range(6):
        resp = requests.post(
            f"{base_url}/api/auth/login",
            json={"email": ADMIN_CREDS["email"], "password": "WrongPassword@123"},
            headers={"x-forwarded-for": unique_ip},
            timeout=30,
        )
        statuses.append(resp.status_code)

    assert statuses[:5] == [401, 401, 401, 401, 401]
    assert statuses[5] == 429


def test_auth_playbook_bcrypt_hash_prefix_2b():
    mongo_client, db = _mongo_db()
    try:
        admin = db.users.find_one({"email": ADMIN_CREDS["email"]}, {"_id": 0, "password_hash": 1})
        assert admin is not None
        assert str(admin.get("password_hash") or "").startswith("$2b$")
    finally:
        mongo_client.close()


def test_auth_playbook_seed_admin_updates_existing_password_if_changed():
    backend_path = Path("/app/backend")
    if str(backend_path) not in os.sys.path:
        os.sys.path.append(str(backend_path))

    from server import _hash_password, _verify_password, seed_auth_and_indexes  # noqa: WPS433

    mongo_client, db = _mongo_db()
    try:
        row = db.users.find_one({"email": ADMIN_CREDS["email"]}, {"_id": 0, "id": 1, "password_hash": 1})
        if not row:
            pytest.skip("Admin seed não encontrado")

        admin_id = str(row["id"])
        original_hash = str(row.get("password_hash") or "")
        db.users.update_one({"id": admin_id}, {"$set": {"password_hash": _hash_password("TmpWrong@123")}})

        asyncio.run(seed_auth_and_indexes())

        refreshed = db.users.find_one({"id": admin_id}, {"_id": 0, "password_hash": 1})
        assert refreshed and _verify_password(ADMIN_CREDS["password"], str(refreshed.get("password_hash") or ""))

        if original_hash:
            db.users.update_one({"id": admin_id}, {"$set": {"password_hash": original_hash}})
    finally:
        mongo_client.close()
