import os
import re
import uuid
from datetime import datetime, timedelta, timezone

import bcrypt
import pytest
import requests
from dotenv import dotenv_values
from pymongo import MongoClient


# Module coverage: anti-disintermediation APIs, protected chat blocking/logging/flags, matching penalty, and auth playbook checks


def _mongo_db():
    backend_env = dotenv_values("/app/backend/.env")
    mongo_url = os.environ.get("MONGO_URL") or backend_env.get("MONGO_URL")
    db_name = os.environ.get("DB_NAME") or backend_env.get("DB_NAME")
    if not mongo_url or not db_name:
        pytest.skip("MONGO_URL/DB_NAME não configurados")
    client = MongoClient(str(mongo_url).strip().strip('"'))
    db = client[str(db_name).strip().strip('"')]
    return client, db


def _clear_login_attempt(email: str):
    mongo_client, db = _mongo_db()
    try:
        db.login_attempts.delete_many({"identifier": {"$regex": f":{email.lower().strip()}$"}})
    finally:
        mongo_client.close()


def _login(base_url: str, email: str, password: str) -> tuple[requests.Session, dict, requests.Response]:
    _clear_login_attempt(email)
    session = requests.Session()
    response = session.post(
        f"{base_url}/api/auth/login",
        json={"email": email, "password": password},
        timeout=25,
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    token = payload.get("access_token")
    assert token
    session.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/json"})
    me_response = session.get(f"{base_url}/api/auth/me", timeout=25)
    assert me_response.status_code == 200, me_response.text
    return session, me_response.json(), response


@pytest.fixture()
def iter40_cleanup_scope():
    scope = {
        "conversation_ids": [],
        "walk_ids": [],
        "temp_user_ids": [],
        "reset_user_ids": set(),
        "reset_user_emails": set(),
    }
    yield scope

    mongo_client, db = _mongo_db()
    try:
        if scope["conversation_ids"]:
            db.anti_disintermediation_events.delete_many({"conversation_id": {"$in": scope["conversation_ids"]}})
            db.protected_chat_messages.delete_many({"conversation_id": {"$in": scope["conversation_ids"]}})

        if scope["walk_ids"]:
            db.walks.delete_many({"id": {"$in": scope["walk_ids"]}})

        if scope["temp_user_ids"]:
            db.users.delete_many({"id": {"$in": scope["temp_user_ids"]}})
            db.notifications.delete_many({"user_id": {"$in": scope["temp_user_ids"]}})

        for user_id in scope["reset_user_ids"]:
            db.users.update_one(
                {"id": user_id},
                {
                    "$set": {
                        "flag_suspeita_desintermediacao": False,
                        "desintermediacao_flag_reason": None,
                        "desintermediacao_flagged_at": None,
                        "desintermediacao_flag_expires_at": None,
                        "disintermediation_limited_until": None,
                        "isActive": True,
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    }
                },
            )
            db.anti_disintermediation_events.delete_many({"user_id": user_id, "event_type": "CONTACT_ATTEMPT"})

        for email in scope["reset_user_emails"]:
            db.login_attempts.delete_many({"identifier": {"$regex": f":{str(email).lower()}$"}})
    finally:
        mongo_client.close()


def test_auth_bcrypt_hash_and_http_only_cookies(base_url):
    mongo_client, db = _mongo_db()
    try:
        admin = db.users.find_one({"email": "admin@petpasso.com"}, {"_id": 0, "password_hash": 1})
    finally:
        mongo_client.close()

    assert admin and isinstance(admin.get("password_hash"), str)
    assert admin["password_hash"].startswith("$2b$")

    _, me_data, login_response = _login(base_url, "admin@petpasso.com", "Admin@123")
    assert me_data.get("email") == "admin@petpasso.com"
    set_cookie = ", ".join(login_response.headers.get_all("Set-Cookie") if hasattr(login_response.headers, "get_all") else [login_response.headers.get("Set-Cookie", "")])
    assert "access_token=" in set_cookie
    assert "refresh_token=" in set_cookie
    assert "HttpOnly" in set_cookie


def test_auth_bruteforce_lockout_after_five_failures(base_url, iter40_cleanup_scope):
    session = requests.Session()
    email = f"iter40_lock_{uuid.uuid4().hex[:8]}@petpasso.com"
    iter40_cleanup_scope["reset_user_emails"].add(email)
    fixed_ip = "203.0.113.44"
    for _ in range(5):
        response = session.post(
            f"{base_url}/api/auth/login",
            json={"email": email, "password": "senha-invalida"},
            headers={"x-forwarded-for": fixed_ip},
            timeout=20,
        )
        assert response.status_code == 401

    lock_response = session.post(
        f"{base_url}/api/auth/login",
        json={"email": email, "password": "senha-invalida"},
        headers={"x-forwarded-for": fixed_ip},
        timeout=20,
    )
    assert lock_response.status_code == 429


def test_auth_cors_preflight_allows_credentials_with_explicit_origin(base_url):
    response = requests.options(
        f"{base_url}/api/auth/login",
        headers={
            "Origin": base_url,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
        timeout=20,
    )
    assert response.status_code in (200, 204)
    assert response.headers.get("Access-Control-Allow-Origin") == base_url
    assert response.headers.get("Access-Control-Allow-Credentials") == "true"


def test_walkers_name_is_masked_for_public_and_client_context(base_url):
    public_response = requests.get(f"{base_url}/api/walkers", timeout=25)
    assert public_response.status_code == 200
    public_rows = public_response.json()
    assert isinstance(public_rows, list) and len(public_rows) > 0

    client_session, _, _ = _login(base_url, "cliente@petpasso.com", "Cliente@123")
    admin_session, _, _ = _login(base_url, "admin@petpasso.com", "Admin@123")

    client_rows = client_session.get(f"{base_url}/api/walkers", timeout=25).json()
    admin_rows = admin_session.get(f"{base_url}/api/walkers", timeout=25).json()

    client_by_id = {row.get("id"): row for row in client_rows}
    admin_by_id = {row.get("id"): row for row in admin_rows}
    common_ids = [wid for wid in client_by_id.keys() if wid in admin_by_id]
    assert common_ids

    masked_diff_found = False
    for walker_id in common_ids:
        client_name = str(client_by_id[walker_id].get("name") or "")
        admin_name = str(admin_by_id[walker_id].get("name") or "")
        if client_name != admin_name and " " in admin_name:
            assert re.match(r"^\S+\s[A-ZÀ-Ý]\.$", client_name)
            masked_diff_found = True
            break
    assert masked_diff_found


def test_walkers_listing_has_rotative_highlight_without_numeric_ranking(base_url):
    session, _, _ = _login(base_url, "cliente@petpasso.com", "Cliente@123")
    response = session.get(
        f"{base_url}/api/walkers",
        params={"date": datetime.now(timezone.utc).strftime("%Y-%m-%d"), "duration_minutes": 30},
        timeout=25,
    )
    assert response.status_code == 200, response.text
    rows = response.json()
    assert rows

    top_count = 0
    reasons = set()
    for row in rows:
        keys = set(row.keys())
        assert "ranking" not in keys
        assert "rank" not in keys
        assert "position" not in keys
        if bool(row.get("is_top_match")):
            top_count += 1
        reason = str(row.get("selection_reason") or "")
        if reason:
            reasons.add(reason)
    assert top_count <= 1
    assert any(reason in reasons for reason in {"Destaques da semana", "Passeadores em alta", "Recomendados na sua região"})


def test_protected_chat_blocks_contact_message_and_returns_security_warning(base_url):
    session, me, _ = _login(base_url, "cliente@petpasso.com", "Cliente@123")
    conversation_id = f"iter40-chat-{uuid.uuid4().hex[:8]}"

    blocked_response = session.post(
        f"{base_url}/api/chat/protected/send",
        json={"conversation_id": conversation_id, "message": "Me chama no 71999998888 ou email teste@dominio.com"},
        timeout=25,
    )
    assert blocked_response.status_code == 200
    blocked_body = blocked_response.json()
    assert blocked_body.get("blocked") is True
    assert blocked_body.get("sent") is False
    assert "segurança" in str(blocked_body.get("warning_message") or "").lower()

    allowed_response = session.post(
        f"{base_url}/api/chat/protected/send",
        json={"conversation_id": conversation_id, "message": "Vamos confirmar o horário aqui no app."},
        timeout=25,
    )
    assert allowed_response.status_code == 200
    allowed_body = allowed_response.json()
    assert allowed_body.get("blocked") is False
    assert allowed_body.get("sent") is True
    assert allowed_body.get("message", {}).get("sender_user_id") == me.get("id")


def test_blocked_chat_attempt_is_logged_in_anti_disintermediation_events(base_url, iter40_cleanup_scope):
    session, me, _ = _login(base_url, "cliente@petpasso.com", "Cliente@123")
    conversation_id = f"iter40-log-{uuid.uuid4().hex[:8]}"
    iter40_cleanup_scope["conversation_ids"].append(conversation_id)

    response = session.post(
        f"{base_url}/api/chat/protected/send",
        json={"conversation_id": conversation_id, "message": "Meu email é teste@teste.com"},
        timeout=25,
    )
    assert response.status_code == 200
    assert response.json().get("blocked") is True

    mongo_client, db = _mongo_db()
    try:
        row = db.anti_disintermediation_events.find_one(
            {"conversation_id": conversation_id, "user_id": me.get("id"), "event_type": "CONTACT_ATTEMPT"},
            {"_id": 0},
        )
    finally:
        mongo_client.close()

    assert row is not None
    assert isinstance(row.get("block_reasons"), list)
    assert row.get("counted_for_threshold") in (True, False)


def test_auto_flag_after_three_blocked_attempts_and_debounce_same_conversation(base_url, iter40_cleanup_scope):
    session, me, _ = _login(base_url, "cliente@petpasso.com", "Cliente@123")
    user_id = str(me.get("id"))
    iter40_cleanup_scope["reset_user_ids"].add(user_id)

    mongo_client, db = _mongo_db()
    try:
        db.anti_disintermediation_events.delete_many({"user_id": user_id, "event_type": "CONTACT_ATTEMPT"})
        db.users.update_one(
            {"id": user_id},
            {
                "$set": {
                    "flag_suspeita_desintermediacao": False,
                    "desintermediacao_flag_reason": None,
                    "desintermediacao_flagged_at": None,
                    "desintermediacao_flag_expires_at": None,
                }
            },
        )
    finally:
        mongo_client.close()

    same_conversation = f"iter40-debounce-{uuid.uuid4().hex[:8]}"
    iter40_cleanup_scope["conversation_ids"].append(same_conversation)
    for _ in range(2):
        response = session.post(
            f"{base_url}/api/chat/protected/send",
            json={"conversation_id": same_conversation, "message": "Me chama no 71988887777"},
            timeout=25,
        )
        assert response.status_code == 200
        assert response.json().get("blocked") is True

    for idx in range(2):
        conversation_id = f"iter40-threshold-{idx}-{uuid.uuid4().hex[:6]}"
        iter40_cleanup_scope["conversation_ids"].append(conversation_id)
        response = session.post(
            f"{base_url}/api/chat/protected/send",
            json={"conversation_id": conversation_id, "message": "Contato externo 71977776666"},
            timeout=25,
        )
        assert response.status_code == 200
        assert response.json().get("blocked") is True

    mongo_client, db = _mongo_db()
    try:
        user_row = db.users.find_one({"id": user_id}, {"_id": 0})
        debounce_events = list(
            db.anti_disintermediation_events.find(
                {"user_id": user_id, "conversation_id": same_conversation, "event_type": "CONTACT_ATTEMPT"},
                {"_id": 0, "counted_for_threshold": 1, "created_at": 1},
            )
        )
    finally:
        mongo_client.close()

    assert user_row is not None
    assert user_row.get("flag_suspeita_desintermediacao") is True
    assert user_row.get("desintermediacao_flag_reason") == "CONTACT_ATTEMPT"

    counted_true = len([ev for ev in debounce_events if bool(ev.get("counted_for_threshold"))])
    counted_false = len([ev for ev in debounce_events if not bool(ev.get("counted_for_threshold"))])
    assert counted_true >= 1
    assert counted_false >= 1


def test_auto_flag_for_cancel_rate_above_40_percent_in_14_days(base_url, iter40_cleanup_scope):
    test_user_id = f"TEST_ITER40_CANCEL_USER_{uuid.uuid4().hex[:8]}"
    test_email = f"iter40_cancel_{uuid.uuid4().hex[:8]}@petpasso.com"
    test_password = "Iter40@123"
    now_iso = datetime.now(timezone.utc).isoformat()
    password_hash = bcrypt.hashpw(test_password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

    mongo_client, db = _mongo_db()
    try:
        db.users.insert_one(
            {
                "id": test_user_id,
                "full_name": "TEST Iter40 Cancel Rate",
                "email": test_email,
                "password_hash": password_hash,
                "role": "cliente",
                "isAdmin": False,
                "isActive": True,
                "accepted_terms": True,
                "accepted_privacy": True,
                "accepted_lgpd": True,
                "created_at": now_iso,
                "updated_at": now_iso,
            }
        )
    finally:
        mongo_client.close()

    iter40_cleanup_scope["temp_user_ids"].append(test_user_id)
    iter40_cleanup_scope["reset_user_emails"].add(test_email)

    session, me, _ = _login(base_url, test_email, test_password)
    user_id = str(me.get("id"))

    walk_docs = []
    for idx, status in enumerate(["Cancelado", "Cancelado", "Finalizado", "Finalizado", "Agendado"]):
        walk_id = f"TEST_ITER40_DISINT_CANCEL_{uuid.uuid4().hex[:10]}_{idx}"
        iter40_cleanup_scope["walk_ids"].append(walk_id)
        walk_docs.append(
            {
                "id": walk_id,
                "client_user_id": user_id,
                "walker_user_id": "walker-test-id",
                "created_at": now_iso,
                "status": status,
                "cancellation_justified_by_system": False,
                "cancellation_justified_by_admin": False,
            }
        )

    mongo_client, db = _mongo_db()
    try:
        db.walks.insert_many(walk_docs)
        db.users.update_one(
            {"id": user_id},
            {
                "$set": {
                    "flag_suspeita_desintermediacao": False,
                    "desintermediacao_flag_reason": None,
                    "desintermediacao_flagged_at": None,
                    "desintermediacao_flag_expires_at": None,
                }
            },
        )
    finally:
        mongo_client.close()

    trigger_response = session.get(f"{base_url}/api/walkers", timeout=25)
    assert trigger_response.status_code == 200

    mongo_client, db = _mongo_db()
    try:
        refreshed_user = db.users.find_one({"id": user_id}, {"_id": 0})
    finally:
        mongo_client.close()

    assert refreshed_user
    assert refreshed_user.get("flag_suspeita_desintermediacao") is True
    assert refreshed_user.get("desintermediacao_flag_reason") == "HIGH_CANCEL_RATE"


def test_matching_penalty_applies_when_disintermediation_flag_is_active(base_url, iter40_cleanup_scope):
    admin_session, _, _ = _login(base_url, "admin@petpasso.com", "Admin@123")

    mongo_client, db = _mongo_db()
    try:
        walker_user = db.users.find_one({"email": "passeador@petpasso.com"}, {"_id": 0, "id": 1})
    finally:
        mongo_client.close()

    if not walker_user:
        pytest.skip("Passeador seed não encontrado")

    walker_user_id = str(walker_user.get("id"))
    walker_public_id = f"partner-{walker_user_id}"
    iter40_cleanup_scope["reset_user_ids"].add(walker_user_id)

    mongo_client, db = _mongo_db()
    try:
        db.users.update_one(
            {"id": walker_user_id},
            {
                "$set": {
                    "flag_suspeita_desintermediacao": False,
                    "desintermediacao_flag_expires_at": None,
                    "desintermediacao_flag_reason": None,
                }
            },
        )
    finally:
        mongo_client.close()

    baseline = admin_session.get(f"{base_url}/api/walkers", timeout=25)
    assert baseline.status_code == 200
    baseline_row = next((row for row in baseline.json() if row.get("id") == walker_public_id), None)
    if not baseline_row:
        pytest.skip("Passeador alvo não apareceu no ranking para comparação de penalidade")
    baseline_score = float(baseline_row.get("match_score") or 0.0)

    future_expiry = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
    mongo_client, db = _mongo_db()
    try:
        db.users.update_one(
            {"id": walker_user_id},
            {
                "$set": {
                    "flag_suspeita_desintermediacao": True,
                    "desintermediacao_flag_reason": "CONTACT_ATTEMPT",
                    "desintermediacao_flag_expires_at": future_expiry,
                }
            },
        )
    finally:
        mongo_client.close()

    flagged = admin_session.get(f"{base_url}/api/walkers", timeout=25)
    assert flagged.status_code == 200
    flagged_row = next((row for row in flagged.json() if row.get("id") == walker_public_id), None)
    assert flagged_row is not None

    flagged_score = float(flagged_row.get("match_score") or 0.0)
    assert (baseline_score - flagged_score) >= 1.5


def test_admin_overview_lists_suspected_users_with_metrics(base_url):
    admin_session, _, _ = _login(base_url, "admin@petpasso.com", "Admin@123")
    response = admin_session.get(f"{base_url}/api/admin/disintermediation/overview", timeout=25)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert "total_flagged_users" in payload
    assert "total_contact_attempts_7d" in payload
    assert isinstance(payload.get("users"), list)
    if payload["users"]:
        sample = payload["users"][0]
        assert "user_id" in sample
        assert "contact_attempts_7d" in sample
        assert "cancel_rate_14d" in sample


def test_admin_disintermediation_actions_warn_limit_suspend_clear_flag(base_url, iter40_cleanup_scope):
    admin_session, _, _ = _login(base_url, "admin@petpasso.com", "Admin@123")

    test_user_id = f"TEST_ITER40_USER_{uuid.uuid4().hex[:10]}"
    now_iso = datetime.now(timezone.utc).isoformat()
    iter40_cleanup_scope["temp_user_ids"].append(test_user_id)

    mongo_client, db = _mongo_db()
    try:
        db.users.insert_one(
            {
                "id": test_user_id,
                "full_name": "TEST Iter40 User",
                "email": f"test_iter40_{uuid.uuid4().hex[:8]}@petpasso.com",
                "password_hash": "",
                "role": "cliente",
                "isAdmin": False,
                "isActive": True,
                "created_at": now_iso,
                "updated_at": now_iso,
                "flag_suspeita_desintermediacao": True,
                "desintermediacao_flag_reason": "CONTACT_ATTEMPT",
                "desintermediacao_flagged_at": now_iso,
                "desintermediacao_flag_expires_at": (datetime.now(timezone.utc) + timedelta(days=7)).isoformat(),
            }
        )
        before_notifications = db.notifications.count_documents({"user_id": test_user_id})
    finally:
        mongo_client.close()

    warn = admin_session.post(
        f"{base_url}/api/admin/disintermediation/{test_user_id}/action",
        json={"action": "warn"},
        timeout=25,
    )
    assert warn.status_code == 200
    assert warn.json().get("ok") is True

    limit = admin_session.post(
        f"{base_url}/api/admin/disintermediation/{test_user_id}/action",
        json={"action": "limit"},
        timeout=25,
    )
    assert limit.status_code == 200

    suspend = admin_session.post(
        f"{base_url}/api/admin/disintermediation/{test_user_id}/action",
        json={"action": "suspend"},
        timeout=25,
    )
    assert suspend.status_code == 200

    clear_flag = admin_session.post(
        f"{base_url}/api/admin/disintermediation/{test_user_id}/action",
        json={"action": "clear_flag"},
        timeout=25,
    )
    assert clear_flag.status_code == 200

    mongo_client, db = _mongo_db()
    try:
        user_row = db.users.find_one({"id": test_user_id}, {"_id": 0})
        after_notifications = db.notifications.count_documents({"user_id": test_user_id})
    finally:
        mongo_client.close()

    assert user_row is not None
    assert user_row.get("isActive") is False
    assert user_row.get("disintermediation_limited_until")
    assert user_row.get("flag_suspeita_desintermediacao") is False
    assert after_notifications >= before_notifications + 1