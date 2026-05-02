import os
import uuid
from pathlib import Path
from typing import Dict, List, Tuple

import pytest
import requests
from dotenv import dotenv_values
from pymongo import MongoClient


# Module coverage: referral program admin settings, referral apply/generate flows, anti-abuse and reward progression.


def _mongo_db():
    backend_env = dotenv_values("/app/backend/.env")
    mongo_url = os.environ.get("MONGO_URL") or backend_env.get("MONGO_URL")
    db_name = os.environ.get("DB_NAME") or backend_env.get("DB_NAME")
    if not mongo_url or not db_name:
        pytest.skip("MONGO_URL/DB_NAME não configurados")
    client = MongoClient(str(mongo_url).strip().strip('"'))
    db = client[str(db_name).strip().strip('"')]
    return client, db


def _login(base_url: str, email: str, password: str, headers: Dict[str, str] | None = None) -> requests.Session:
    session = requests.Session()
    request_headers = headers or {}
    response = session.post(
        f"{base_url}/api/auth/login",
        json={"email": email, "password": password},
        headers=request_headers,
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
    if request_headers:
        session.headers.update(request_headers)
    return session


def _register_temp_client(base_url: str, marker: str, ip_octet: int | None = None) -> Tuple[str, str]:
    email = f"testrf_{marker}_{uuid.uuid4().hex[:8]}@example.com"
    password = "Cliente@123"
    headers = {
        "x-forwarded-for": f"198.51.100.{ip_octet if ip_octet is not None else (20 + int(uuid.uuid4().hex[:2], 16) % 200)}",
        "x-device-id": f"reg-{uuid.uuid4().hex[:10]}",
    }
    response = requests.post(
        f"{base_url}/api/auth/register",
        json={
            "full_name": f"TEST RF {marker}",
            "email": email,
            "password": password,
            "role": "cliente",
            "accepted_terms": True,
            "accepted_privacy": True,
            "accepted_lgpd": True,
        },
        headers=headers,
        timeout=35,
    )
    assert response.status_code in (200, 201), response.text
    return email, password


def _safe_close(session: requests.Session | None):
    if session is not None:
        session.close()


@pytest.fixture()
def referral_scope(base_url: str):
    admin = _login(base_url, "superadmin@petpasso.com", "SuperAdmin@123")
    original_settings = admin.get(f"{base_url}/api/admin/referral-program/settings", timeout=30)
    assert original_settings.status_code == 200, original_settings.text

    state: Dict[str, object] = {
        "admin": admin,
        "original_settings": original_settings.json(),
        "created_emails": [],
        "created_walk_ids": [],
        "created_payment_ids": [],
        "lock_identifier": None,
    }

    yield state

    restore_payload = {
        "program_enabled": bool(state["original_settings"].get("program_enabled", False)),
        "client_referral_enabled": bool(state["original_settings"].get("client_referral_enabled", False)),
        "walker_referral_enabled": bool(state["original_settings"].get("walker_referral_enabled", False)),
        "app_visible": bool(state["original_settings"].get("app_visible", False)),
        "client_rules": state["original_settings"].get("client_rules", {}),
        "walker_rules": state["original_settings"].get("walker_rules", {}),
    }
    admin.patch(
        f"{base_url}/api/admin/referral-program/settings",
        json=restore_payload,
        timeout=35,
    )

    mongo_client, db = _mongo_db()
    try:
        created_emails: List[str] = list(state["created_emails"])
        if created_emails:
            users = list(db.users.find({"email": {"$in": created_emails}}, {"_id": 0, "id": 1}))
            user_ids = [str(item.get("id") or "") for item in users if item.get("id")]
            if user_ids:
                db.referrals.delete_many(
                    {
                        "$or": [
                            {"referred_user_id": {"$in": user_ids}},
                            {"referrer_user_id": {"$in": user_ids}},
                        ]
                    }
                )
                db.referral_codes.delete_many({"owner_user_id": {"$in": user_ids}})
                db.coupons.delete_many({"target_user_id": {"$in": user_ids}})
            db.users.delete_many({"email": {"$in": created_emails}})

        created_walk_ids: List[str] = list(state["created_walk_ids"])
        if created_walk_ids:
            db.walks.delete_many({"id": {"$in": created_walk_ids}})
        created_payment_ids: List[str] = list(state["created_payment_ids"])
        if created_payment_ids:
            db.payments.delete_many({"id": {"$in": created_payment_ids}})

        if state.get("lock_identifier"):
            db.login_attempts.delete_many({"identifier": state["lock_identifier"]})
    finally:
        mongo_client.close()
        admin.close()


def _enable_all_referrals(admin: requests.Session, base_url: str, min_paid_walks: int = 2) -> Dict[str, object]:
    response = admin.patch(
        f"{base_url}/api/admin/referral-program/settings",
        json={
            "program_enabled": True,
            "client_referral_enabled": True,
            "walker_referral_enabled": True,
            "app_visible": True,
            "client_rules": {
                "indicated_discount_amount": 25,
                "referrer_coupon_credit_amount": 30,
                "min_paid_walks_for_referrer_bonus": min_paid_walks,
                "referral_limit_per_user": 20,
                "benefit_validity_days": 45,
            },
            "walker_rules": {
                "fixed_bonus_amount": 120,
                "min_completed_walks": 2,
                "min_rating_required": 4.0,
                "max_no_show_rate": 10,
                "eligibility_window_days": 60,
            },
        },
        timeout=35,
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_auth_bcrypt_hash_starts_with_2b_prefix():
    mongo_client, db = _mongo_db()
    try:
        admin_row = db.users.find_one({"email": "superadmin@petpasso.com"}, {"_id": 0, "password_hash": 1})
    finally:
        mongo_client.close()

    assert admin_row is not None
    assert str(admin_row.get("password_hash") or "").startswith("$2b$")


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


def test_auth_lockout_after_five_failed_attempts(base_url: str, referral_scope):
    test_ip = "203.0.113.91"
    identifier = f"{test_ip}:superadmin@petpasso.com"
    referral_scope["lock_identifier"] = identifier

    mongo_client, db = _mongo_db()
    try:
        db.login_attempts.delete_many({"identifier": identifier})
    finally:
        mongo_client.close()

    for _ in range(5):
        response = requests.post(
            f"{base_url}/api/auth/login",
            json={"email": "superadmin@petpasso.com", "password": "wrong-pass"},
            headers={"x-forwarded-for": test_ip},
            timeout=30,
        )
        assert response.status_code == 401

    locked = requests.post(
        f"{base_url}/api/auth/login",
        json={"email": "superadmin@petpasso.com", "password": "wrong-pass"},
        headers={"x-forwarded-for": test_ip},
        timeout=30,
    )
    assert locked.status_code == 429


def test_admin_referral_settings_toggle_and_rules_persist(base_url: str, referral_scope):
    admin = referral_scope["admin"]

    updated = _enable_all_referrals(admin, base_url, min_paid_walks=2)
    assert updated["program_enabled"] is True
    assert updated["client_referral_enabled"] is True
    assert updated["walker_referral_enabled"] is True
    assert updated["app_visible"] is True
    assert float(updated["client_rules"]["indicated_discount_amount"]) == 25
    assert float(updated["walker_rules"]["fixed_bonus_amount"]) == 120

    fetched = admin.get(f"{base_url}/api/admin/referral-program/settings", timeout=30)
    assert fetched.status_code == 200, fetched.text
    payload = fetched.json()
    assert payload["program_enabled"] is True
    assert int(payload["client_rules"]["min_paid_walks_for_referrer_bonus"]) == 2
    assert int(payload["walker_rules"]["eligibility_window_days"]) == 60


def test_admin_referrals_list_and_manual_status_patch(base_url: str, referral_scope):
    admin = referral_scope["admin"]
    _enable_all_referrals(admin, base_url, min_paid_walks=2)

    client = _login(base_url, "cliente@petpasso.com", "Cliente@123")
    temp_email = temp_password = None
    temp_session = None
    try:
        gen = client.post(f"{base_url}/api/referrals/my-code/generate", timeout=30)
        assert gen.status_code == 200, gen.text
        code = str(gen.json().get("referral_code") or "")
        assert code.startswith("PET-")

        temp_email, temp_password = _register_temp_client(base_url, "audit", ip_octet=31)
        referral_scope["created_emails"].append(temp_email)
        temp_session = _login(
            base_url,
            temp_email,
            temp_password,
            headers={"x-device-id": f"rf-{uuid.uuid4().hex[:8]}", "x-forwarded-for": "198.51.100.31"},
        )

        applied = temp_session.post(
            f"{base_url}/api/referrals/apply",
            json={"referral_code": code},
            timeout=30,
        )
        assert applied.status_code == 201, applied.text
        referral_id = applied.json()["id"]

        listed = admin.get(f"{base_url}/api/admin/referrals?limit=50", timeout=30)
        assert listed.status_code == 200, listed.text
        ids = {item["id"] for item in listed.json().get("items", [])}
        assert referral_id in ids

        patched = admin.patch(
            f"{base_url}/api/admin/referrals/{referral_id}/status",
            json={"status": "invalida_fraude", "note": "TEST manual status patch"},
            timeout=30,
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["status"] == "invalida_fraude"
    finally:
        _safe_close(client)
        _safe_close(temp_session)


def test_user_cliente_generate_code_and_dashboard(base_url: str, referral_scope):
    _enable_all_referrals(referral_scope["admin"], base_url, min_paid_walks=2)

    client = _login(base_url, "cliente@petpasso.com", "Cliente@123")
    try:
        generated = client.post(f"{base_url}/api/referrals/my-code/generate", timeout=30)
        assert generated.status_code == 200, generated.text
        dashboard = generated.json()
        referral_code = str(dashboard.get("referral_code") or "")
        assert referral_code.startswith("PET-")
        assert len(referral_code) == 8
        assert dashboard.get("role") == "cliente"
        assert dashboard.get("program_enabled") is True
        assert dashboard.get("role_enabled") is True
    finally:
        _safe_close(client)


def test_user_walker_generate_code_and_dashboard(base_url: str, referral_scope):
    _enable_all_referrals(referral_scope["admin"], base_url, min_paid_walks=2)

    walker = _login(base_url, "passeador@petpasso.com", "Passeador@123")
    try:
        generated = walker.post(f"{base_url}/api/referrals/my-code/generate", timeout=30)
        assert generated.status_code == 200, generated.text
        dashboard = generated.json()
        referral_code = str(dashboard.get("referral_code") or "")
        assert referral_code.startswith("DOG-")
        assert len(referral_code) == 8
        assert dashboard.get("role") == "passeador"
        assert dashboard.get("role_enabled") is True
    finally:
        _safe_close(walker)


def test_apply_code_creates_correct_referral_for_client_and_walker(base_url: str, referral_scope):
    admin = referral_scope["admin"]
    _enable_all_referrals(admin, base_url, min_paid_walks=2)

    client_referrer = _login(base_url, "cliente@petpasso.com", "Cliente@123")
    walker_referrer = _login(base_url, "passeador@petpasso.com", "Passeador@123")
    referred_client_session = referred_walker_session = None

    try:
        client_code = client_referrer.post(f"{base_url}/api/referrals/my-code/generate", timeout=30).json()["referral_code"]
        walker_code = walker_referrer.post(f"{base_url}/api/referrals/my-code/generate", timeout=30).json()["referral_code"]

        temp_email, temp_password = _register_temp_client(base_url, "applymix", ip_octet=41)
        referral_scope["created_emails"].append(temp_email)
        referred_client_session = _login(
            base_url,
            temp_email,
            temp_password,
            headers={"x-device-id": f"rf-{uuid.uuid4().hex[:8]}", "x-forwarded-for": "198.51.100.41"},
        )
        client_apply = referred_client_session.post(
            f"{base_url}/api/referrals/apply",
            json={"referral_code": client_code},
            timeout=30,
        )
        assert client_apply.status_code == 201, client_apply.text
        assert client_apply.json()["referral_type"] == "cliente_para_cliente"
        assert client_apply.json()["status"] in {"pendente_ativacao", "invalida_fraude"}

        referred_walker_session = _login(
            base_url,
            "walker@petpasso.com",
            "Walker@123",
            headers={"x-device-id": f"rf-{uuid.uuid4().hex[:8]}", "x-forwarded-for": "198.51.100.42"},
        )
        walker_apply = referred_walker_session.post(
            f"{base_url}/api/referrals/apply",
            json={"referral_code": walker_code},
            timeout=30,
        )
        assert walker_apply.status_code == 201, walker_apply.text
        assert walker_apply.json()["referral_type"] == "passeador_para_passeador"
        assert walker_apply.json()["status"] in {"pendente_ativacao", "invalida_fraude"}
    finally:
        _safe_close(client_referrer)
        _safe_close(walker_referrer)
        _safe_close(referred_client_session)
        _safe_close(referred_walker_session)


def test_referral_disabled_blocks_generate_and_apply(base_url: str, referral_scope):
    admin = referral_scope["admin"]
    updated = admin.patch(
        f"{base_url}/api/admin/referral-program/settings",
        json={
            "program_enabled": False,
            "client_referral_enabled": False,
            "walker_referral_enabled": False,
            "app_visible": False,
        },
        timeout=30,
    )
    assert updated.status_code == 200, updated.text

    client = _login(base_url, "cliente@petpasso.com", "Cliente@123")
    temp_session = None
    try:
        generate = client.post(f"{base_url}/api/referrals/my-code/generate", timeout=30)
        assert generate.status_code == 403

        temp_email, temp_password = _register_temp_client(base_url, "disabled", ip_octet=51)
        referral_scope["created_emails"].append(temp_email)
        temp_session = _login(base_url, temp_email, temp_password)
        apply = temp_session.post(
            f"{base_url}/api/referrals/apply",
            json={"referral_code": "PET-ABCD"},
            timeout=30,
        )
        assert apply.status_code == 403
    finally:
        _safe_close(client)
        _safe_close(temp_session)


def test_antiabuse_blocks_self_referral_and_flags_repeated_device(base_url: str, referral_scope):
    _enable_all_referrals(referral_scope["admin"], base_url, min_paid_walks=2)
    client = _login(base_url, "cliente@petpasso.com", "Cliente@123")
    try:
        code = client.post(f"{base_url}/api/referrals/my-code/generate", timeout=30).json()["referral_code"]

        self_apply = client.post(
            f"{base_url}/api/referrals/apply",
            json={"referral_code": code},
            timeout=30,
        )
        assert self_apply.status_code == 400

        shared_device = "rf-shared-device"
        for idx in range(4):
            temp_email, temp_password = _register_temp_client(base_url, f"abuse{idx}", ip_octet=61 + idx)
            referral_scope["created_emails"].append(temp_email)
            temp = _login(
                base_url,
                temp_email,
                temp_password,
                headers={"x-device-id": shared_device, "x-forwarded-for": f"198.51.100.{61 + idx}"},
            )
            try:
                applied = temp.post(
                    f"{base_url}/api/referrals/apply",
                    json={"referral_code": code},
                    timeout=30,
                )
                assert applied.status_code == 201, applied.text
                if idx < 3:
                    assert applied.json()["status"] == "pendente_ativacao"
                else:
                    assert applied.json()["status"] == "invalida_fraude"
                    assert "device_limit" in applied.json().get("fraud_flags", [])
            finally:
                temp.close()
    finally:
        client.close()


def test_referral_progress_updates_and_reward_released_after_paid_event(base_url: str, referral_scope):
    admin = referral_scope["admin"]
    settings = _enable_all_referrals(admin, base_url, min_paid_walks=1)

    referrer = _login(base_url, "cliente@petpasso.com", "Cliente@123")
    referred = None
    try:
        code = referrer.post(f"{base_url}/api/referrals/my-code/generate", timeout=30).json()["referral_code"]
        temp_email, temp_password = _register_temp_client(base_url, "reward", ip_octet=90)
        referral_scope["created_emails"].append(temp_email)
        referred = _login(
            base_url,
            temp_email,
            temp_password,
            headers={"x-device-id": f"rf-{uuid.uuid4().hex[:8]}", "x-forwarded-for": "198.51.100.90"},
        )

        apply = referred.post(
            f"{base_url}/api/referrals/apply",
            json={"referral_code": code},
            timeout=30,
        )
        assert apply.status_code == 201, apply.text
        referral_id = apply.json()["id"]

        me = referred.get(f"{base_url}/api/auth/me", timeout=30)
        assert me.status_code == 200, me.text
        referred_user_id = me.json().get("id")
        assert referred_user_id

        walk_id = f"TEST_REF_WALK_{uuid.uuid4().hex[:8]}"
        payment_id = f"TEST_REF_PAY_{uuid.uuid4().hex[:8]}"
        referral_scope["created_walk_ids"].append(walk_id)
        referral_scope["created_payment_ids"].append(payment_id)

        mongo_client, db = _mongo_db()
        try:
            db.walks.insert_one(
                {
                    "id": walk_id,
                    "client_user_id": referred_user_id,
                    "walker_user_id": "walker-seed",
                    "client_name": "TEST RF reward",
                    "walker_name": "TEST Walker",
                    "participant_user_ids": [],
                    "payment_status": "Pendente",
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "updated_at": "2026-01-01T00:00:00+00:00",
                }
            )
            db.payments.insert_one(
                {
                    "id": payment_id,
                    "walk_id": walk_id,
                    "client_name": "TEST RF reward",
                    "plan_type": "Avulso",
                    "tipoPlano": "avulso",
                    "value": 49.0,
                    "payment_status": "Pendente",
                    "payment_method": "",
                    "tipoPagamento": "",
                    "payment_date": None,
                    "notes": "",
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "updated_at": "2026-01-01T00:00:00+00:00",
                }
            )
        finally:
            mongo_client.close()

        paid = admin.patch(
            f"{base_url}/api/admin/payments/{payment_id}/status",
            json={"payment_status": "Pago", "payment_method": "pix", "notes": "TEST referral payment"},
            timeout=35,
        )
        assert paid.status_code == 200, paid.text

        listed = admin.get(f"{base_url}/api/admin/referrals?limit=200", timeout=35)
        assert listed.status_code == 200, listed.text
        target = next((row for row in listed.json().get("items", []) if row.get("id") == referral_id), None)
        assert target is not None
        assert target["status"] == "recompensa_liberada"
        assert float(target.get("reward_amount") or 0) == float(settings["client_rules"]["referrer_coupon_credit_amount"])
        assert target.get("reward_released_at")
        assert int((target.get("condition_progress") or {}).get("paid_walks") or 0) >= 1
    finally:
        _safe_close(referrer)
        _safe_close(referred)
