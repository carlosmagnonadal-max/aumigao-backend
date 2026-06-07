import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import requests
from dotenv import dotenv_values
from pymongo import MongoClient


# Module coverage: P0 walker quality payload + P1 coupon validation, redemption persistence and per-user limits.
class TestP0P1CouponsQuality:
    def _auth_session(self, base_url: str, email: str, password: str) -> requests.Session:
        session = requests.Session()
        session.headers.update({"Accept": "application/json", "Content-Type": "application/json"})
        login = session.post(
            f"{base_url}/api/auth/login",
            json={"email": email, "password": password},
            timeout=20,
        )
        assert login.status_code == 200, login.text
        token = login.json().get("access_token")
        assert token
        session.headers.update({"Authorization": f"Bearer {token}"})
        return session

    def _future_date(self, days: int = 2) -> str:
        target = datetime.now(timezone.utc) + timedelta(days=days)
        if target.weekday() >= 5:
            target += timedelta(days=(7 - target.weekday()))
        return target.strftime("%Y-%m-%d")

    def _next_available_slot(self, session: requests.Session, base_url: str, walker_id: str, duration_minutes: int) -> tuple[str, str]:
        for day_offset in range(1, 20):
            date_value = self._future_date(days=day_offset)
            response = session.get(
                f"{base_url}/api/walkers/{walker_id}/availability-slots",
                params={"date": date_value, "duration_minutes": duration_minutes},
                timeout=20,
            )
            if response.status_code != 200:
                continue
            slots = response.json().get("available_slots", [])
            if slots:
                return date_value, slots[0]
        raise AssertionError("Nenhum horário disponível encontrado para o teste")

    def _create_coupon(self, api_client: requests.Session, base_url: str, *, max_uses_per_user: int = 2) -> str:
        code = f"TESTP1{uuid.uuid4().hex[:8]}".upper()
        response = api_client.post(
            f"{base_url}/api/admin/coupons",
            json={
                "code": code,
                "discount_percent": 10,
                "discount_fixed": 5,
                "max_global_uses": 50,
                "max_uses_per_user": max_uses_per_user,
                "applicable_walk_types": ["Individual", "Compartilhado"],
                "is_active": True,
            },
            timeout=20,
        )
        assert response.status_code == 201, response.text
        return code

    def _create_client_pet(self, client_session: requests.Session, base_url: str, suffix: str) -> str:
        pet_response = client_session.post(
            f"{base_url}/api/pets",
            json={
                "pet_name": f"TEST_ITER24_PET_{suffix}_{uuid.uuid4().hex[:5]}",
                "behavioral_notes": "TEST comportamento",
                "photo_url": "",
                "owner_name": "",
                "gets_along_with_dogs": True,
                "accepts_shared_walk": True,
                "pet_size": "Médio",
                "energy_level": "Médio",
                "pulls_leash": False,
                "dog_behavior": "Neutro",
            },
            timeout=20,
        )
        assert pet_response.status_code == 201, pet_response.text
        return pet_response.json()["id"]

    def _walk_payload(self, *, pet_id: str, walk_date: str, walk_time: str, coupon_code: str) -> dict:
        return {
            "pet_name": "TEST_ITER24_WALK",
            "pet_id": pet_id,
            "client_name": "TEST_ITER24_CLIENT",
            "walk_date": walk_date,
            "walk_time": walk_time,
            "duration_minutes": 30,
            "walk_type": "Individual",
            "walker_id": "walker-1",
            "pickup_street": "Rua Teste",
            "pickup_number": "10",
            "pickup_neighborhood": "Centro",
            "pickup_complement": "",
            "location_reference": "Praça",
            "coupon_code": coupon_code,
            "pet_behavior_notes": "",
            "notes": "TEST_ITER24_COUPON",
        }

    def test_walker_quality_endpoint_returns_core_fields(self, base_url):
        walker_session = self._auth_session(base_url, "passeador@petpasso.com", "Passeador@123")
        try:
            response = walker_session.get(f"{base_url}/api/walker/quality", timeout=20)
            assert response.status_code == 200
            payload = response.json()
            assert payload["quality_status"] in {"ativo_premium", "ativo", "em_observacao", "restrito", "suspenso"}
            assert isinstance(payload["rating_avg"], (int, float))
            assert isinstance(payload["completed_walks"], int)
            assert "instructions" in payload
        finally:
            walker_session.close()

    def test_coupon_validate_active_for_individual(self, api_client, base_url):
        coupon_code = self._create_coupon(api_client, base_url, max_uses_per_user=2)
        client_session = self._auth_session(base_url, "cliente@petpasso.com", "Cliente@123")
        try:
            response = client_session.post(
                f"{base_url}/api/coupons/validate",
                json={"code": coupon_code, "walk_type": "Individual", "subtotal": 29.9},
                timeout=20,
            )
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["code"] == coupon_code
            assert body["discount_amount"] == 7.99
            assert body["total_after_discount"] == 21.91
            assert "Individual" in body["applicable_walk_types"]
        finally:
            client_session.close()

    def test_create_walk_with_coupon_persists_discount_and_price_fields(self, api_client, base_url):
        coupon_code = self._create_coupon(api_client, base_url, max_uses_per_user=2)
        client_session = self._auth_session(base_url, "cliente@petpasso.com", "Cliente@123")
        try:
            pet_id = self._create_client_pet(client_session, base_url, "PERSIST")
            walk_date, walk_time = self._next_available_slot(client_session, base_url, "walker-1", 30)

            create_response = client_session.post(
                f"{base_url}/api/walks",
                json=self._walk_payload(
                    pet_id=pet_id,
                    walk_date=walk_date,
                    walk_time=walk_time,
                    coupon_code=coupon_code,
                ),
                timeout=20,
            )
            assert create_response.status_code == 201, create_response.text
            created = create_response.json()
            expected_final = round(created["price_before_discount"] - created["discount_amount"], 2)
            assert created["coupon_code"] == coupon_code
            assert created["discount_amount"] > 0
            assert created["base_price"] == expected_final

            persisted = client_session.get(f"{base_url}/api/walks/{created['id']}", timeout=20)
            assert persisted.status_code == 200
            persisted_json = persisted.json()
            assert persisted_json["coupon_code"] == coupon_code
            assert persisted_json["discount_amount"] == created["discount_amount"]
            assert persisted_json["base_price"] == created["base_price"]
        finally:
            client_session.close()

    def test_coupon_max_uses_per_user_blocks_exceeded_usage(self, api_client, base_url):
        coupon_code = self._create_coupon(api_client, base_url, max_uses_per_user=1)
        client_session = self._auth_session(base_url, "cliente@petpasso.com", "Cliente@123")
        try:
            pet_id = self._create_client_pet(client_session, base_url, "LIMIT")

            first_date, first_time = self._next_available_slot(client_session, base_url, "walker-1", 30)
            first_create = client_session.post(
                f"{base_url}/api/walks",
                json=self._walk_payload(
                    pet_id=pet_id,
                    walk_date=first_date,
                    walk_time=first_time,
                    coupon_code=coupon_code,
                ),
                timeout=20,
            )
            assert first_create.status_code == 201, first_create.text

            second_validate = client_session.post(
                f"{base_url}/api/coupons/validate",
                json={"code": coupon_code, "walk_type": "Individual", "subtotal": 29.9},
                timeout=20,
            )
            assert second_validate.status_code == 400
            assert "limite de uso" in second_validate.json().get("detail", "").lower()
        finally:
            client_session.close()


# Module coverage: auth playbook checks for bcrypt/hash format, cookies, CORS credentials, lockout protection.
def _mongo_client_and_db():
    backend_env = dotenv_values("/app/backend/.env")
    mongo_url = os.environ.get("MONGO_URL") or backend_env.get("MONGO_URL")
    db_name = os.environ.get("DB_NAME") or backend_env.get("DB_NAME")
    if not mongo_url or not db_name:
        pytest.skip("MONGO_URL/DB_NAME not configured")
    return MongoClient(str(mongo_url).strip().strip('"')), str(db_name).strip().strip('"')


def test_auth_playbook_bcrypt_seed_hash_prefix_2b():
    mongo, db_name = _mongo_client_and_db()
    try:
        admin = mongo[db_name].users.find_one({"email": "admin@petpasso.com"}, {"password_hash": 1, "_id": 0})
        assert admin is not None
        assert str(admin.get("password_hash", "")).startswith("$2b$")
    finally:
        mongo.close()


def test_auth_playbook_login_sets_httponly_cookies(base_url):
    response = requests.post(
        f"{base_url}/api/auth/login",
        json={"email": "admin@petpasso.com", "password": "Admin@123"},
        timeout=20,
    )
    assert response.status_code == 200
    set_cookie = response.headers.get("set-cookie", "")
    assert "HttpOnly" in set_cookie
    assert "access_token" in set_cookie
    assert "refresh_token" in set_cookie


def test_auth_playbook_cors_preflight_allows_credentials_and_explicit_origin(base_url):
    origin = base_url.rstrip("/")
    response = requests.options(
        f"{base_url}/api/auth/login",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
        timeout=20,
    )
    assert response.status_code in (200, 204)
    assert response.headers.get("access-control-allow-origin") == origin
    assert response.headers.get("access-control-allow-credentials") == "true"


def test_auth_playbook_lockout_after_five_failures(base_url):
    session = requests.Session()
    email = f"lockout_iter24_{uuid.uuid4().hex[:6]}@petpasso.com"
    payload = {"email": email, "password": "wrong-password"}
    try:
        for _ in range(5):
            response = session.post(f"{base_url}/api/auth/login", json=payload, timeout=20)
            assert response.status_code in (401, 429)

        blocked = session.post(f"{base_url}/api/auth/login", json=payload, timeout=20)
        assert blocked.status_code == 429
    finally:
        session.close()