"""Testes de product_url e upload de foto da Vitrine de Destaques (mig 0095).

Cobre:
  - product_url aceito no create e devolvido nos GETs (admin + tutor);
  - product_url aceito no patch e devolvido;
  - product_url inválido rejeitado (ftp://, texto solto) com 422;
  - upload happy path (mock do object_storage — padrão do projeto);
  - upload com content_type inválido rejeitado com 400;
  - upload sem autenticação rejeitado com 401.
"""
from __future__ import annotations

import io
from datetime import datetime
from unittest.mock import patch

import app.models  # noqa: F401

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.dependencies.auth import get_current_user, require_admin
from app.models.tenant import Tenant, TenantFeature
from app.models.tenant_product_highlight import TenantProductHighlight
from app.models.user import User
from app.routes import product_highlights as routes

# PNG 1×1 válido (magic bytes corretos — passa pelo read_image_upload_safely).
PNG_1x1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6360000002000100ffff03000006000557bfabd400"
    "00000049454e44ae426082"
)


# ---------------------------------------------------------------------------
# Fixtures compartilhadas
# ---------------------------------------------------------------------------

def _ctx(*, plan: str = "enterprise", toggle: bool = True):
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    db.add(Tenant(id="t1", name="T1", slug="t1", status="active", plan=plan))
    db.add(User(id="adm1", email="a1@x.com", password_hash="x", role="admin", tenant_id="t1"))
    db.add(User(id="tut1", email="t1u@x.com", password_hash="x", role="tutor", tenant_id="t1"))
    if toggle:
        db.add(TenantFeature(tenant_id="t1", feature_key="product_highlights", enabled=True))
    db.commit()
    return db


def _client(db, user, monkeypatch):
    monkeypatch.setenv("PRODUCT_HIGHLIGHTS_ENABLED", "true")
    monkeypatch.setenv("PRICING_V2_ENABLED", "true")
    app = FastAPI()
    app.include_router(routes.api_router)
    app.include_router(routes.api_tutor_router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: user
    # require_admin também usa get_current_user internamente.
    app.dependency_overrides[require_admin] = lambda: user
    return TestClient(app)


# ---------------------------------------------------------------------------
# product_url — create, patch, GETs (admin + tutor)
# ---------------------------------------------------------------------------

def test_create_with_product_url_accepted_and_returned(monkeypatch):
    db = _ctx()
    c = _client(db, db.get(User, "adm1"), monkeypatch)
    r = c.post("/api/admin/product-highlights", json={
        "title": "Coleira GPS",
        "product_url": "https://loja.exemplo.com/coleira-gps",
    })
    assert r.status_code == 201, r.text
    item = r.json()["item"]
    assert item["product_url"] == "https://loja.exemplo.com/coleira-gps"


def test_product_url_returned_in_admin_list(monkeypatch):
    db = _ctx()
    c = _client(db, db.get(User, "adm1"), monkeypatch)
    c.post("/api/admin/product-highlights", json={
        "title": "Ração Premium",
        "product_url": "http://pet.com/racao",
    })
    items = c.get("/api/admin/product-highlights").json()["items"]
    assert len(items) == 1
    assert items[0]["product_url"] == "http://pet.com/racao"


def test_product_url_returned_in_tutor_get(monkeypatch):
    db = _ctx()
    c_adm = _client(db, db.get(User, "adm1"), monkeypatch)
    c_adm.post("/api/admin/product-highlights", json={
        "title": "Combo Banho",
        "product_url": "https://agenda.loja.com/banho",
    })
    c_tut = _client(db, db.get(User, "tut1"), monkeypatch)
    items = c_tut.get("/api/product-highlights").json()["items"]
    assert len(items) == 1
    assert items[0]["product_url"] == "https://agenda.loja.com/banho"


def test_patch_product_url_updates_and_returns(monkeypatch):
    db = _ctx()
    c = _client(db, db.get(User, "adm1"), monkeypatch)
    hid = c.post("/api/admin/product-highlights", json={"title": "Item A"}).json()["item"]["id"]
    r = c.patch(f"/api/admin/product-highlights/{hid}", json={
        "product_url": "https://loja.com/item-a",
    })
    assert r.status_code == 200, r.text
    assert r.json()["item"]["product_url"] == "https://loja.com/item-a"


def test_product_url_null_by_default(monkeypatch):
    db = _ctx()
    c = _client(db, db.get(User, "adm1"), monkeypatch)
    r = c.post("/api/admin/product-highlights", json={"title": "Sem link"})
    assert r.status_code == 201
    assert r.json()["item"]["product_url"] is None


# ---------------------------------------------------------------------------
# product_url — validação (inválido → 422)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_url", [
    "ftp://loja.com/produto",
    "loja.com/produto",
    "produto sem protocolo",
    "file:///etc/passwd",
    "javascript:alert(1)",
])
def test_invalid_product_url_rejected_on_create(monkeypatch, bad_url):
    db = _ctx()
    c = _client(db, db.get(User, "adm1"), monkeypatch)
    r = c.post("/api/admin/product-highlights", json={
        "title": "X",
        "product_url": bad_url,
    })
    assert r.status_code == 422, f"Esperava 422 para {bad_url!r}, got {r.status_code}"


@pytest.mark.parametrize("bad_url", [
    "ftp://loja.com/produto",
    "texto solto",
])
def test_invalid_product_url_rejected_on_patch(monkeypatch, bad_url):
    db = _ctx()
    c = _client(db, db.get(User, "adm1"), monkeypatch)
    hid = c.post("/api/admin/product-highlights", json={"title": "X"}).json()["item"]["id"]
    r = c.patch(f"/api/admin/product-highlights/{hid}", json={"product_url": bad_url})
    assert r.status_code == 422, f"Esperava 422 para {bad_url!r}, got {r.status_code}"


# ---------------------------------------------------------------------------
# Upload de foto — happy path
# ---------------------------------------------------------------------------

def _upload_client(db, user, monkeypatch):
    """Cliente com require_admin sobrescrito, para testar o endpoint de upload."""
    monkeypatch.setenv("PRODUCT_HIGHLIGHTS_ENABLED", "true")
    monkeypatch.setenv("PRICING_V2_ENABLED", "true")
    app = FastAPI()
    app.include_router(routes.api_router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[require_admin] = lambda: user
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app)


def _patched_upload_client(db, user, monkeypatch, tmp_base):
    """Cliente com mocks de storage e root para testes de upload."""
    monkeypatch.setenv("PRODUCT_HIGHLIGHTS_ENABLED", "true")
    monkeypatch.setenv("PRICING_V2_ENABLED", "true")
    monkeypatch.setattr("app.routes.product_highlights._HIGHLIGHT_UPLOAD_ROOT", tmp_base / "ph")
    monkeypatch.setattr("app.routes.product_highlights.UPLOADS_BASE", tmp_base)
    app = FastAPI()
    app.include_router(routes.api_router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[require_admin] = lambda: user
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app)


def test_upload_photo_happy_path(monkeypatch, tmp_path):
    db = _ctx()
    user = db.get(User, "adm1")
    c = _patched_upload_client(db, user, monkeypatch, tmp_path)

    # Mock do object_storage.save para não tocar em disco/R2 real.
    with patch("app.routes.product_highlights.object_storage.save") as mock_save:
        mock_save.return_value = None
        r = c.post(
            "/api/admin/product-highlights/upload-photo",
            files={"file": ("foto.png", io.BytesIO(PNG_1x1), "image/png")},
        )

    assert r.status_code == 201, r.text
    body = r.json()
    assert "photo_url" in body
    assert "product_highlight-" in body["photo_url"]
    assert ".png" in body["photo_url"]


def test_upload_photo_jpeg_accepted(monkeypatch, tmp_path):
    """JPEG com magic bytes corretos deve ser aceito."""
    # JPEG header mínimo válido (FF D8 FF E0 ...).
    jpeg_bytes = bytes.fromhex("ffd8ffe000104a464946000101000048004800") + b"\x00" * 20

    db = _ctx()
    user = db.get(User, "adm1")
    c = _patched_upload_client(db, user, monkeypatch, tmp_path)

    with patch("app.routes.product_highlights.object_storage.save") as mock_save:
        mock_save.return_value = None
        r = c.post(
            "/api/admin/product-highlights/upload-photo",
            files={"file": ("foto.jpg", io.BytesIO(jpeg_bytes), "image/jpeg")},
        )

    assert r.status_code == 201, r.text
    assert ".jpg" in r.json()["photo_url"]


def test_upload_photo_webp_accepted(monkeypatch, tmp_path):
    """WebP (RIFF....WEBP header) deve ser aceito."""
    webp_bytes = b"RIFF\x24\x00\x00\x00WEBP" + b"\x00" * 20

    db = _ctx()
    user = db.get(User, "adm1")
    c = _patched_upload_client(db, user, monkeypatch, tmp_path)

    with patch("app.routes.product_highlights.object_storage.save") as mock_save:
        mock_save.return_value = None
        r = c.post(
            "/api/admin/product-highlights/upload-photo",
            files={"file": ("foto.webp", io.BytesIO(webp_bytes), "image/webp")},
        )

    assert r.status_code == 201, r.text
    assert ".webp" in r.json()["photo_url"]


# ---------------------------------------------------------------------------
# Upload de foto — rejeições
# ---------------------------------------------------------------------------

def test_upload_photo_invalid_content_type_rejected(monkeypatch):
    db = _ctx()
    user = db.get(User, "adm1")
    c = _upload_client(db, user, monkeypatch)

    r = c.post(
        "/api/admin/product-highlights/upload-photo",
        files={"file": ("doc.pdf", io.BytesIO(b"%PDF-1.4 fake content"), "application/pdf")},
    )
    assert r.status_code == 400, r.text
    assert "suportado" in r.json()["detail"].lower() or "tipo" in r.json()["detail"].lower()


def test_upload_photo_gif_content_type_rejected(monkeypatch):
    """GIF não está na lista de content_types aceitos (jpg/png/webp)."""
    db = _ctx()
    user = db.get(User, "adm1")
    c = _upload_client(db, user, monkeypatch)

    r = c.post(
        "/api/admin/product-highlights/upload-photo",
        files={"file": ("ani.gif", io.BytesIO(b"GIF89a" + b"\x00" * 20), "image/gif")},
    )
    assert r.status_code == 400, r.text


def test_upload_photo_without_auth_returns_401(monkeypatch):
    """Sem override de require_admin → deve retornar 401."""
    monkeypatch.setenv("PRODUCT_HIGHLIGHTS_ENABLED", "true")
    app = FastAPI()
    app.include_router(routes.api_router)
    # SEM dependency_overrides para require_admin → HTTPBearer auto_error → 401.
    c = TestClient(app, raise_server_exceptions=False)
    r = c.post(
        "/api/admin/product-highlights/upload-photo",
        files={"file": ("foto.png", io.BytesIO(PNG_1x1), "image/png")},
    )
    assert r.status_code == 401, r.text
