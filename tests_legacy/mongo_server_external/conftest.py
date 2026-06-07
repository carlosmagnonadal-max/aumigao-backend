import os
from pathlib import Path

import pytest
import requests
from dotenv import dotenv_values
from pymongo import MongoClient


def _resolve_base_url() -> str:
    def _normalize_for_test(raw_url: str) -> str:
        normalized = raw_url.rstrip("/")
        if ".preview.emergentagent.com" in normalized:
            return "http://127.0.0.1:8001"
        return normalized

    env_url = os.environ.get("EXPO_BACKEND_URL")
    if env_url:
        return _normalize_for_test(env_url)

    frontend_env = Path("/app/frontend/.env")
    if frontend_env.exists():
        values = dotenv_values(frontend_env)
        public_url = values.get("EXPO_BACKEND_URL") or values.get("EXPO_PUBLIC_BACKEND_URL")
        if public_url:
            return _normalize_for_test(str(public_url))

    raise RuntimeError("EXPO_BACKEND_URL is not configured")


@pytest.fixture(scope="session")
def base_url() -> str:
    return _resolve_base_url()


@pytest.fixture()
def api_client(base_url: str):
    session = requests.Session()
    session.headers.update({"Accept": "application/json"})

    admin_email = "superadmin@petpasso.com"
    login_payload = {"email": admin_email, "password": "SuperAdmin@123"}

    login_response = session.post(
        f"{base_url}/api/auth/login",
        json=login_payload,
        timeout=20,
    )

    if login_response.status_code == 429:
        backend_env = Path("/app/backend/.env")
        backend_values = dotenv_values(backend_env) if backend_env.exists() else {}
        mongo_url = os.environ.get("MONGO_URL") or backend_values.get("MONGO_URL")
        db_name = os.environ.get("DB_NAME") or backend_values.get("DB_NAME")
        if mongo_url and db_name:
            mongo = MongoClient(str(mongo_url).strip().strip('"'))
            try:
                mongo[str(db_name).strip().strip('"')].login_attempts.delete_many(
                    {"identifier": {"$regex": f":{admin_email}$"}}
                )
            finally:
                mongo.close()

            login_response = session.post(
                f"{base_url}/api/auth/login",
                json=login_payload,
                timeout=20,
            )

    if login_response.ok:
        token = login_response.json().get("access_token")
        if token:
            session.headers.update({"Authorization": f"Bearer {token}"})

    yield session
    session.close()


@pytest.fixture(scope="session", autouse=True)
def cleanup_test_seed_data():
    backend_env = Path("/app/backend/.env")
    backend_values = dotenv_values(backend_env) if backend_env.exists() else {}

    mongo_url = os.environ.get("MONGO_URL") or backend_values.get("MONGO_URL")
    db_name = os.environ.get("DB_NAME") or backend_values.get("DB_NAME")

    if not mongo_url or not db_name:
        yield
        return

    client = MongoClient(str(mongo_url).strip().strip('"'))
    database = client[str(db_name).strip().strip('"')]

    def _purge_test_data():
        database.walks.delete_many(
            {
                "$or": [
                    {"pet_name": {"$regex": r"^TEST"}},
                    {"client_name": {"$regex": r"^TEST"}},
                    {"notes": {"$regex": r"^TEST"}},
                ]
            }
        )
        database.pet_profiles.delete_many({"pet_name": {"$regex": r"^TEST"}})
        database.owner_profiles.delete_many({"full_name": {"$regex": r"^TEST"}})
        database.partner_applications.delete_many({"full_name": {"$regex": r"^TEST"}})
        database.payments.delete_many({"$or": [{"client_name": {"$regex": r"^TEST"}}, {"notes": {"$regex": r"^TEST"}}]})

    _purge_test_data()
    yield
    _purge_test_data()
    client.close()
