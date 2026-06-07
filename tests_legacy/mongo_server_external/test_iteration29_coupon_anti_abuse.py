import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import requests
from dotenv import dotenv_values
from pymongo import MongoClient


# Module coverage: anti-abuse for register/device/ip, coupon identity/ip/global limits and admin antifraud endpoints.
class TestIteration29CouponAntiAbuse:
    def _mongo_db(self):
        backend_env = dotenv_values("/app/backend/.env")
        mongo_url = os.environ.get("MONGO_URL") or backend_env.get("MONGO_URL")
        db_name = os.environ.get("DB_NAME") or backend_env.get("DB_NAME")
        if not mongo_url or not db_name:
            pytest.skip("MONGO_URL/DB_NAME not configured")
        client = MongoClient(str(mongo_url).strip().strip('"'))
        return client, client[str(db_name).strip().strip('"')]

    @pytest.fixture(autouse=True)
    def cleanup_iteration29_data(self):
        prefix = "TEST_ITER29"
        yield
        mongo_client, db = self._mongo_db()
        try:
            users = list(db.users.find({"email": {"$regex": f"^{prefix}"}}, {"id": 1, "email": 1, "_id": 0}))
            user_ids = [str(item.get("id")) for item in users if item.get("id")]
            emails = [str(item.get("email")) for item in users if item.get("email")]

            db.walks.delete_many({
                "$or": [
                    {"notes": {"$regex": f"^{prefix}"}},
                    {"pet_name": {"$regex": f"^{prefix}"}},
                    {"client_name": {"$regex": f"^{prefix}"}},
                    {"client_user_id": {"$in": user_ids}} if user_ids else {"id": "__none__"},
                ]
            })
            db.payments.delete_many({"notes": {"$regex": f"^{prefix}"}})
            db.pets.delete_many({"pet_name": {"$regex": f"^{prefix}"}})
            db.owner_profiles.delete_many({
                "$or": [
                    {"full_name": {"$regex": f"^{prefix}"}},
                    {"email": {"$in": emails}} if emails else {"id": "__none__"},
                    {"user_id": {"$in": user_ids}} if user_ids else {"id": "__none__"},
                ]
            })
            db.users.delete_many({"email": {"$regex": f"^{prefix}"}})
            db.coupons.delete_many({"code": {"$regex": f"^{prefix}"}})
            db.coupon_redemptions.delete_many({
                "$or": [
                    {"coupon_code": {"$regex": f"^{prefix}"}},
                    {"user_email": {"$regex": f"^{prefix}"}},
                ]
            })
            db.coupon_fraud_alerts.delete_many({
                "$or": [
                    {"coupon_code": {"$regex": f"^{prefix}"}},
                    {"user_email": {"$regex": f"^{prefix}"}},
                ]
            })
            db.anti_abuse_blocks.delete_many({"reason": {"$regex": "Excesso|Uso de cupom acima do limite|múltiplas contas|multiplas contas", "$options": "i"}})
            db.login_attempts.delete_many({"identifier": {"$regex": f"{prefix.lower()}"}})
        finally:
            mongo_client.close()

    def _auth_session(self, base_url: str, email: str, password: str, *, ip: str = "", device_id: str = "") -> requests.Session:
        session = requests.Session()
        session.headers.update({"Accept": "application/json", "Content-Type": "application/json"})
        if ip:
            session.headers.update({"X-Forwarded-For": ip})
        if device_id:
            session.headers.update({"X-Device-ID": device_id})

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

    def _register_client(self, base_url: str, *, email: str, ip: str, device_id: str, password: str = "Teste@123"):
        session = requests.Session()
        session.headers.update(
            {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-Forwarded-For": ip,
                "X-Device-ID": device_id,
            }
        )
        payload = {
            "full_name": f"TEST_ITER29 {email.split('@')[0]}",
            "email": email,
            "password": password,
            "role": "cliente",
            "accepted_terms": True,
            "accepted_privacy": True,
            "accepted_lgpd": True,
        }
        response = session.post(f"{base_url}/api/auth/register", json=payload, timeout=20)
        if response.status_code == 201:
            token = response.json().get("access_token")
            if token:
                session.headers.update({"Authorization": f"Bearer {token}"})
        return session, response

    def _create_coupon(self, api_client: requests.Session, base_url: str, *, max_global_uses: int = 20, max_uses_per_user: int = 5):
        code = f"TEST_ITER29_{uuid.uuid4().hex[:8]}".upper()
        response = api_client.post(
            f"{base_url}/api/admin/coupons",
            json={
                "code": code,
                "discount_percent": 10,
                "discount_fixed": 3,
                "max_global_uses": max_global_uses,
                "max_uses_per_user": max_uses_per_user,
                "applicable_walk_types": ["Individual"],
                "is_active": True,
            },
            timeout=20,
        )
        assert response.status_code == 201, response.text
        return response.json()

    def _next_slot(self, session: requests.Session, base_url: str, walker_id: str = "walker-1"):
        for offset in range(1, 20):
            date_value = (datetime.now(timezone.utc) + timedelta(days=offset)).strftime("%Y-%m-%d")
            response = session.get(
                f"{base_url}/api/walkers/{walker_id}/availability-slots",
                params={"date": date_value, "duration_minutes": 30},
                timeout=20,
            )
            if response.status_code != 200:
                continue
            slots = response.json().get("available_slots", [])
            if slots:
                return date_value, slots[0]
        raise AssertionError("No available slot found for iteration 29 tests")

    def _create_pet(self, client_session: requests.Session, base_url: str, suffix: str):
        response = client_session.post(
            f"{base_url}/api/pets",
            json={
                "pet_name": f"TEST_ITER29_PET_{suffix}_{uuid.uuid4().hex[:4]}",
                "behavioral_notes": "TEST_ITER29 notes",
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
        assert response.status_code == 201, response.text
        return response.json()["id"]

    def _create_walk_with_coupon(self, client_session: requests.Session, base_url: str, *, pet_id: str, coupon_code: str):
        walk_date, walk_time = self._next_slot(client_session, base_url)
        response = client_session.post(
            f"{base_url}/api/walks",
            json={
                "pet_name": f"TEST_ITER29_WALK_{uuid.uuid4().hex[:5]}",
                "pet_id": pet_id,
                "client_name": "TEST_ITER29_CLIENT",
                "walk_date": walk_date,
                "walk_time": walk_time,
                "duration_minutes": 30,
                "walk_type": "Individual",
                "walker_id": "walker-1",
                "pickup_street": "Rua Teste",
                "pickup_number": "10",
                "pickup_neighborhood": "Centro",
                "pickup_complement": "",
                "location_reference": "TEST_ITER29",
                "coupon_code": coupon_code,
                "pet_behavior_notes": "",
                "notes": f"TEST_ITER29_WALK_NOTE_{uuid.uuid4().hex[:5]}",
            },
            timeout=20,
        )
        return response

    def test_register_limit_max_3_accounts_per_device(self, base_url):
        device_id = f"iter29-device-{uuid.uuid4().hex[:10]}"
        sessions = []
        try:
            for idx in range(1, 5):
                email = f"TEST_ITER29_device_{idx}_{uuid.uuid4().hex[:6]}@mail.com"
                session, response = self._register_client(
                    base_url,
                    email=email,
                    ip=f"177.10.10.{idx}",
                    device_id=device_id,
                )
                sessions.append(session)
                if idx <= 3:
                    assert response.status_code == 201, response.text
                else:
                    assert response.status_code == 429
                    assert "temporariamente indisponível" in response.json().get("detail", "").lower()
        finally:
            for session in sessions:
                session.close()

    def test_register_limit_max_4_registrations_per_ip_and_block_next(self, base_url):
        ip = f"188.22.{datetime.now(timezone.utc).minute}.{datetime.now(timezone.utc).second}"
        sessions = []
        try:
            for idx in range(1, 6):
                email = f"TEST_ITER29_ip_{idx}_{uuid.uuid4().hex[:6]}@mail.com"
                session, response = self._register_client(
                    base_url,
                    email=email,
                    ip=ip,
                    device_id=f"iter29-ip-device-{idx}-{uuid.uuid4().hex[:5]}",
                )
                sessions.append(session)
                if idx <= 4:
                    assert response.status_code == 201, response.text
                else:
                    assert response.status_code == 429
                    assert "temporariamente indisponível" in response.json().get("detail", "").lower()

            blocked_email = f"TEST_ITER29_ip_blocked_{uuid.uuid4().hex[:6]}@mail.com"
            blocked_session, blocked_response = self._register_client(
                base_url,
                email=blocked_email,
                ip=ip,
                device_id=f"iter29-ip-after-block-{uuid.uuid4().hex[:5]}",
            )
            sessions.append(blocked_session)
            assert blocked_response.status_code == 429
        finally:
            for session in sessions:
                session.close()

    def test_coupon_ip_third_use_same_ip_blocked_with_friendly_message(self, api_client, base_url):
        coupon = self._create_coupon(api_client, base_url, max_global_uses=20, max_uses_per_user=5)
        ip = f"201.45.{datetime.now(timezone.utc).minute}.{datetime.now(timezone.utc).second}"

        users = []
        try:
            for idx in range(1, 4):
                email = f"TEST_ITER29_coupon_ip_{idx}_{uuid.uuid4().hex[:6]}@mail.com"
                register_session, register_response = self._register_client(
                    base_url,
                    email=email,
                    ip=ip,
                    device_id=f"iter29-coupon-device-{idx}-{uuid.uuid4().hex[:5]}",
                )
                assert register_response.status_code == 201, register_response.text
                users.append(register_session)

            for idx, session in enumerate(users, start=1):
                pet_id = self._create_pet(session, base_url, f"IP{idx}")
                walk_response = self._create_walk_with_coupon(
                    session,
                    base_url,
                    pet_id=pet_id,
                    coupon_code=coupon["code"],
                )
                if idx <= 2:
                    assert walk_response.status_code == 201, walk_response.text
                else:
                    assert walk_response.status_code == 400
                    assert walk_response.json().get("detail") == "Cupom inválido ou já utilizado"
        finally:
            for session in users:
                session.close()

    def test_coupon_identity_limit_per_user_returns_limit_message(self, api_client, base_url):
        coupon = self._create_coupon(api_client, base_url, max_global_uses=20, max_uses_per_user=1)
        email = f"TEST_ITER29_identity_{uuid.uuid4().hex[:6]}@mail.com"
        client_session, register_response = self._register_client(
            base_url,
            email=email,
            ip=f"170.20.{datetime.now(timezone.utc).minute}.10",
            device_id=f"iter29-identity-{uuid.uuid4().hex[:6]}",
        )
        assert register_response.status_code == 201, register_response.text

        try:
            pet_id = self._create_pet(client_session, base_url, "IDENTITY")
            first_walk = self._create_walk_with_coupon(client_session, base_url, pet_id=pet_id, coupon_code=coupon["code"])
            assert first_walk.status_code == 201, first_walk.text

            validate_again = client_session.post(
                f"{base_url}/api/coupons/validate",
                json={"code": coupon["code"], "walk_type": "Individual", "subtotal": 39.9},
                timeout=20,
            )
            assert validate_again.status_code == 400
            assert validate_again.json().get("detail") == "Limite de uso atingido"
        finally:
            client_session.close()

    def test_coupon_global_limit_auto_deactivates(self, api_client, base_url):
        coupon = self._create_coupon(api_client, base_url, max_global_uses=1, max_uses_per_user=5)
        email = f"TEST_ITER29_global_{uuid.uuid4().hex[:6]}@mail.com"
        client_session, register_response = self._register_client(
            base_url,
            email=email,
            ip=f"171.30.{datetime.now(timezone.utc).minute}.20",
            device_id=f"iter29-global-{uuid.uuid4().hex[:6]}",
        )
        assert register_response.status_code == 201, register_response.text

        try:
            pet_id = self._create_pet(client_session, base_url, "GLOBAL")
            walk_response = self._create_walk_with_coupon(client_session, base_url, pet_id=pet_id, coupon_code=coupon["code"])
            assert walk_response.status_code == 201, walk_response.text

            list_response = api_client.get(f"{base_url}/api/admin/coupons", timeout=20)
            assert list_response.status_code == 200, list_response.text
            row = next((item for item in list_response.json() if item.get("id") == coupon["id"]), None)
            assert row is not None
            assert row["used_count"] >= 1
            assert row["is_active"] is False
        finally:
            client_session.close()

    def test_admin_antifraud_endpoints_overview_block_user_and_invalidate_coupon(self, api_client, base_url):
        coupon = self._create_coupon(api_client, base_url, max_global_uses=5, max_uses_per_user=5)
        email = f"TEST_ITER29_adminflow_{uuid.uuid4().hex[:6]}@mail.com"
        client_session, register_response = self._register_client(
            base_url,
            email=email,
            ip=f"172.40.{datetime.now(timezone.utc).minute}.30",
            device_id=f"iter29-adminflow-{uuid.uuid4().hex[:6]}",
        )
        assert register_response.status_code == 201, register_response.text
        user_id = register_response.json()["user"]["id"]

        try:
            overview = api_client.get(f"{base_url}/api/admin/coupons/anti-abuse/overview", timeout=20)
            assert overview.status_code == 200
            body = overview.json()
            assert isinstance(body.get("alerts"), list)
            assert isinstance(body.get("usage_by_ip"), list)

            invalidate = api_client.patch(f"{base_url}/api/admin/coupons/{coupon['id']}/invalidate", timeout=20)
            assert invalidate.status_code == 200, invalidate.text
            assert invalidate.json().get("is_active") is False

            validate_after_invalidate = client_session.post(
                f"{base_url}/api/coupons/validate",
                json={"code": coupon["code"], "walk_type": "Individual", "subtotal": 25},
                timeout=20,
            )
            assert validate_after_invalidate.status_code == 400
            assert validate_after_invalidate.json().get("detail") == "Cupom inválido ou já utilizado"

            block_user = api_client.patch(
                f"{base_url}/api/admin/coupons/anti-abuse/users/{user_id}/block",
                timeout=20,
            )
            assert block_user.status_code == 200
            assert "bloqueado" in block_user.json().get("message", "").lower()

            replacement_coupon = self._create_coupon(api_client, base_url, max_global_uses=5, max_uses_per_user=5)
            validate_blocked_user = client_session.post(
                f"{base_url}/api/coupons/validate",
                json={"code": replacement_coupon["code"], "walk_type": "Individual", "subtotal": 25},
                timeout=20,
            )
            assert validate_blocked_user.status_code == 400
            assert validate_blocked_user.json().get("detail") == "Cupom inválido ou já utilizado"
        finally:
            client_session.close()

    def test_coupon_invalid_message_is_user_friendly(self, base_url):
        session = self._auth_session(base_url, "cliente@petpasso.com", "Cliente@123", ip="190.80.10.10", device_id="iter29-msg")
        try:
            response = session.post(
                f"{base_url}/api/coupons/validate",
                json={"code": "INVALIDO_TEST_ITER29", "walk_type": "Individual", "subtotal": 15},
                timeout=20,
            )
            assert response.status_code == 400
            assert response.json().get("detail") == "Cupom inválido ou já utilizado"
        finally:
            session.close()
