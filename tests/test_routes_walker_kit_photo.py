"""WK-07 — upload REAL das fotos do kit (multipart -> URL hospedada).

Antes o app só guardava fotos do kit localmente (file://), e o WK-05 passou a
rejeitar file:// no submit. Faltava o endpoint que recebe a foto e devolve uma URL
http hospedada para o app enviar no kit. Este é esse endpoint, espelhando o pipeline
de upload já usado em completion-photo (validação de imagem + object_storage + registry).
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
WALKER_ID = "walker-kit"

PNG_1x1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6360000002000100ffff03000006000557bfabd400"
    "00000049454e44ae426082"
)


def _build(authed=True):
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
    if authed:
        test_app.dependency_overrides[get_current_user] = lambda: db.get(User, WALKER_ID)
    return TestClient(test_app), db


def test_kit_photo_upload_returns_hosted_http_url():
    client, _ = _build()
    r = client.post("/walker/kit/photo", files={"file": ("k.png", PNG_1x1, "image/png")})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert isinstance(body["photo_url"], str) and body["photo_url"].startswith("http")
    # nunca devolve file:// — é o ponto do WK-05/WK-07
    assert not body["photo_url"].startswith("file:")


def test_kit_photo_requires_auth_401():
    client, _ = _build(authed=False)
    r = client.post("/walker/kit/photo", files={"file": ("k.png", PNG_1x1, "image/png")})
    assert r.status_code == 401


def test_kit_photo_rejects_non_image():
    client, _ = _build()
    r = client.post("/walker/kit/photo", files={"file": ("k.txt", b"nao sou imagem", "text/plain")})
    assert r.status_code == 400
