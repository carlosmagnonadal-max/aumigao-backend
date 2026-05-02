import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from dotenv import dotenv_values
from pymongo import MongoClient


# Module coverage: secure defaults for admin coupon create/update and legacy-mode compatibility.
class TestIteration30CouponSafeDefaults:
    @staticmethod
    def _mongo_db():
        backend_env = dotenv_values("/app/backend/.env")
        mongo_url = os.environ.get("MONGO_URL") or backend_env.get("MONGO_URL")
        db_name = os.environ.get("DB_NAME") or backend_env.get("DB_NAME")
        if not mongo_url or not db_name:
            pytest.skip("MONGO_URL/DB_NAME not configured")
        client = MongoClient(str(mongo_url).strip().strip('"'))
        return client, client[str(db_name).strip().strip('"')]

    @pytest.fixture(autouse=True)
    def cleanup_iteration30_data(self):
        yield
        mongo_client, db = self._mongo_db()
        try:
            db.coupons.delete_many({"code": {"$regex": r"^TEST_ITER30"}})
            db.coupon_redemptions.delete_many({"coupon_code": {"$regex": r"^TEST_ITER30"}})
            db.coupon_fraud_alerts.delete_many({"coupon_code": {"$regex": r"^TEST_ITER30"}})
        finally:
            mongo_client.close()

    def _coupon_payload(self, *, code_suffix: str, include_max_per_user: bool = True):
        tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
        next_week = (datetime.now(timezone.utc) + timedelta(days=8)).strftime("%Y-%m-%d")
        payload = {
            "code": f"TEST_ITER30_{code_suffix}_{uuid.uuid4().hex[:6]}".upper(),
            "discount_percent": 15,
            "valid_from": tomorrow,
            "valid_until": next_week,
            "max_global_uses": 20,
            "applicable_walk_types": ["Individual"],
            "is_active": True,
        }
        if include_max_per_user:
            payload["max_uses_per_user"] = 3
        return payload

    def _insert_legacy_coupon(self):
        mongo_client, db = self._mongo_db()
        try:
            coupon_id = str(uuid.uuid4())
            now_iso = datetime.now(timezone.utc).isoformat()
            row = {
                "id": coupon_id,
                "code": f"TEST_ITER30_LEGACY_{uuid.uuid4().hex[:6]}".upper(),
                "discount_percent": 10,
                "discount_fixed": 0,
                "valid_from": None,
                "valid_until": None,
                "max_global_uses": 0,
                "max_uses_per_user": 2,
                "used_count": 0,
                "applicable_walk_types": ["Individual"],
                "is_active": True,
                "created_at": now_iso,
                "updated_at": now_iso,
            }
            db.coupons.insert_one(row)
            return row
        finally:
            mongo_client.close()

    def test_create_coupon_requires_max_global_uses(self, api_client, base_url):
        payload = self._coupon_payload(code_suffix="REQMAX")
        payload.pop("max_global_uses")

        response = api_client.post(f"{base_url}/api/admin/coupons", json=payload, timeout=20)

        assert response.status_code == 400
        assert "Limite global obrigatório" in response.json().get("detail", "")

    def test_create_coupon_requires_validity_window(self, api_client, base_url):
        payload = self._coupon_payload(code_suffix="REQVAL")
        payload.pop("valid_from")

        response = api_client.post(f"{base_url}/api/admin/coupons", json=payload, timeout=20)

        assert response.status_code == 400
        assert "Validade inicial e final" in response.json().get("detail", "")

    def test_create_coupon_defaults_max_uses_per_user_to_one(self, api_client, base_url):
        payload = self._coupon_payload(code_suffix="DEFAULT", include_max_per_user=False)

        create_response = api_client.post(f"{base_url}/api/admin/coupons", json=payload, timeout=20)
        assert create_response.status_code == 201, create_response.text
        created = create_response.json()
        assert created["max_uses_per_user"] == 1

        list_response = api_client.get(f"{base_url}/api/admin/coupons", timeout=20)
        assert list_response.status_code == 200, list_response.text
        persisted = next((row for row in list_response.json() if row.get("id") == created["id"]), None)
        assert persisted is not None
        assert persisted["max_uses_per_user"] == 1

    def test_patch_legacy_coupon_without_global_and_validity_is_blocked(self, api_client, base_url):
        legacy_coupon = self._insert_legacy_coupon()

        patch_response = api_client.patch(
            f"{base_url}/api/admin/coupons/{legacy_coupon['id']}",
            json={"discount_percent": 12},
            timeout=20,
        )

        assert patch_response.status_code == 400
        assert "Limite global obrigatório" in patch_response.json().get("detail", "")

    def test_existing_legacy_coupon_stays_active_and_returns_legacy_mode_true(self, api_client, base_url):
        legacy_coupon = self._insert_legacy_coupon()

        list_response = api_client.get(f"{base_url}/api/admin/coupons", timeout=20)
        assert list_response.status_code == 200, list_response.text
        row = next((item for item in list_response.json() if item.get("id") == legacy_coupon["id"]), None)

        assert row is not None
        assert row["is_active"] is True
        assert row["legacy_mode"] is True
