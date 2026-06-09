from datetime import datetime
from pathlib import Path
from uuid import uuid4
import shutil

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models import *  # noqa: F403
from app.models.user import User
from app.models.walker_profile import WalkerProfile
from app.routes.admin import _serialize_walker_profile
from app.services import signed_uploads


@pytest.fixture()
def uploads_app(monkeypatch):
    test_root = Path(__file__).resolve().parents[1] / ".pytest-upload-tests" / uuid4().hex
    upload_root = test_root / "uploads"
    upload_root.mkdir(parents=True)
    monkeypatch.setattr(signed_uploads, "UPLOAD_ROOT", upload_root)
    monkeypatch.setenv("SIGNED_UPLOAD_SECRET", "test-signed-upload-secret-with-enough-bytes")

    app = FastAPI()

    @app.get("/uploads/{upload_path:path}")
    def serve_upload(upload_path: str, request: Request):
        file_path = signed_uploads.upload_file_path(upload_path)
        if not file_path or not file_path.is_file():
            raise HTTPException(status_code=404, detail="Arquivo nao encontrado")
        if signed_uploads.is_sensitive_upload_path(upload_path) and not signed_uploads.has_valid_upload_signature(
            upload_path,
            request.url.query,
        ):
            raise HTTPException(status_code=403, detail="Assinatura invalida ou expirada")
        return FileResponse(file_path)

    try:
        yield TestClient(app), upload_root
    finally:
        shutil.rmtree(test_root, ignore_errors=True)


def _write_upload(upload_root, relative_path: str, content: bytes = b"image-bytes"):
    destination = upload_root / relative_path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(content)
    return destination


def test_sensitive_walker_document_without_signature_is_not_public(uploads_app):
    client, upload_root = uploads_app
    _write_upload(upload_root, "walker-documents/user/identity_front-secret.jpg")

    response = client.get("/uploads/walker-documents/user/identity_front-secret.jpg")

    assert response.status_code == 403


def test_sensitive_walker_document_with_valid_signature_is_allowed(uploads_app):
    client, upload_root = uploads_app
    _write_upload(upload_root, "walker-documents/user/identity_back-secret.jpg", b"allowed")
    signed_url = signed_uploads.create_signed_upload_url("/uploads/walker-documents/user/identity_back-secret.jpg")

    response = client.get(signed_url)

    assert response.status_code == 200
    assert response.content == b"allowed"


def test_expired_signature_is_blocked(uploads_app):
    client, upload_root = uploads_app
    _write_upload(upload_root, "walker-documents/user/address_proof-secret.jpg")
    signed_url = signed_uploads.create_signed_upload_url(
        "/uploads/walker-documents/user/address_proof-secret.jpg",
        ttl_seconds=-1,
    )

    response = client.get(signed_url)

    assert response.status_code == 403


def test_profile_photo_stays_public(uploads_app):
    client, upload_root = uploads_app
    _write_upload(upload_root, "walker-documents/user/profile_photo-public.jpg", b"profile")

    response = client.get("/uploads/walker-documents/user/profile_photo-public.jpg")

    assert response.status_code == 200
    assert response.content == b"profile"


def test_path_traversal_is_blocked(uploads_app):
    client, _upload_root = uploads_app

    response = client.get("/uploads/walker-documents/user/..%2Fidentity_front-secret.jpg")

    assert response.status_code == 404


def test_admin_serializer_rewrites_sensitive_document_urls(monkeypatch):
    monkeypatch.setenv("SIGNED_UPLOAD_SECRET", "test-signed-upload-secret-with-enough-bytes")
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    db = Session()
    try:
        user = User(
            id="walker-admin-docs",
            email="walker-admin-docs@example.com",
            password_hash="x",
            full_name="Admin Docs",
            role="walker",
            is_active=True,
        )
        profile = WalkerProfile(
            id="profile-admin-docs",
            user_id=user.id,
            full_name="Admin Docs",
            cpf="12345678909",
            phone="11999999999",
            city="Salvador",
            state="Pituba",
            experience="Experiencia",
            bio="Bio longa para auditoria de documento sensivel.",
            profile_photo_url="/uploads/walker-documents/owner/profile_photo-public.jpg",
            document_url="/uploads/walker-documents/owner/identity_front-secret.jpg",
            identity_document_back_url="/uploads/walker-documents/owner/identity_back-secret.jpg",
            selfie_url="/uploads/walker-documents/owner/selfie-secret.jpg",
            proof_of_address_url="/uploads/walker-documents/owner/address_proof-secret.jpg",
            status="submitted",
            active_as_walker=False,
            created_at=datetime.utcnow(),
        )
        db.add_all([user, profile])
        db.commit()

        payload = _serialize_walker_profile(profile, db)

        for key in (
            "document_url",
            "identity_document_front_url",
            "identity_document_back_url",
            "selfie_url",
            "proof_of_address_url",
        ):
            assert "expires=" in payload[key]
            assert "signature=" in payload[key]
        assert payload["profile_photo_url"] == "/uploads/walker-documents/owner/profile_photo-public.jpg"
    finally:
        db.close()
