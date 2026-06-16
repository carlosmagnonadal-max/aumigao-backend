"""WK-05 — kit só aceita fotos HOSPEDADAS (http/https), nunca file:// local.

Antes update_kit persistia photo_urls como vinham (o app mandava file:// locais que
o admin/tutor não conseguem abrir). Agora URIs locais são rejeitadas com 422.
"""
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.core.database import Base, get_db
from app.dependencies.auth import get_current_user
from app.models.tenant import Tenant
from app.models.user import User
from app.models.walker_profile import WalkerProfile
from app.routes import walker
from app.services.tenant_seed_service import DEFAULT_TENANT_SLUG

TENANT_ID = "t-test"
WALKER_ID = "walker-test"


def build():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(Tenant(id=TENANT_ID, name="Aumigao", slug=DEFAULT_TENANT_SLUG, status="active", plan="business"))
    db.add(User(id=WALKER_ID, email="w@x.com", password_hash="x", role="walker", tenant_id=TENANT_ID, full_name="P"))
    db.add(WalkerProfile(id="wp-a", user_id=WALKER_ID, full_name="P", status="active", active_as_walker=True))
    db.commit()
    test_app = FastAPI()
    test_app.include_router(walker.router)
    test_app.dependency_overrides[get_db] = lambda: db
    test_app.dependency_overrides[get_current_user] = lambda: db.get(User, WALKER_ID)
    return TestClient(test_app), db


def _put(client, photo_urls=None, available=True):
    item = {"key": "water", "available": available}
    if photo_urls is not None:
        item["photo_urls"] = photo_urls
    return client.put("/walker/kit", json={"items": [item]})


def test_kit_rejects_local_file_uri():
    client, _ = build()
    r = _put(client, photo_urls=["file:///data/user/0/x.jpg"])
    assert r.status_code == 422


def test_kit_rejects_content_uri():
    client, _ = build()
    r = _put(client, photo_urls=["content://media/external/images/1"])
    assert r.status_code == 422


def test_kit_accepts_hosted_https_url():
    client, _ = build()
    r = _put(client, photo_urls=["https://cdn.aumigao/kit/x.jpg"])
    assert r.status_code == 200, r.text


def test_kit_available_without_photos_ok():
    client, _ = build()
    r = _put(client, available=True)
    assert r.status_code == 200, r.text
