"""Testes de segurança de upload — suite U6.

Cobre todos os controles endurecidos em 2026-06-21:
- G3: extensão inválida → 400
- G7: limite por tipo (5 MB foto / 10 MB doc) → 413
- G6: documento aceita PDF; foto rejeita PDF → 400
- U3/G2: rate limit em E2 (pets), E3 (kit/photo), E4 (completion-photo) → 429
- G9: document_url externa em BackgroundCertificatePayload → 422
- Regressão: imagem válida aceita nos três endpoints autenticados.

Padrão do projeto: FastAPI mínimo + SQLite StaticPool + dependency_overrides.
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest
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
from app.models.walk import Walk
from app.models.walker_profile import WalkerProfile
from app.routes import partner_application, pets, walker
from app.services import upload_validation as uv
from app.services.tenant_seed_service import DEFAULT_TENANT_SLUG

# ---------------------------------------------------------------------------
# Constantes de magic bytes para fixtures
# ---------------------------------------------------------------------------
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 100
PNG_1x1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c6360000002000100ffff030000060005"
) + b"\x00" * 20  # magic bytes válidos + padding
PDF_BYTES = b"%PDF-1.4 fake content" + b"\x00" * 50
EXE_BYTES = b"MZ\x90\x00\x03\x00\x00\x00" + b"\x00" * 100  # PE header
HTML_BYTES = b"<html><body>xss</body></html>"
SVG_BYTES = b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>"

TENANT_ID = "t-sec"
WALKER_ID = "walker-sec"
OTHER_WALKER_ID = "walker-other"
WALK_ID = "walk-sec"


# ---------------------------------------------------------------------------
# Fixtures de banco e app
# ---------------------------------------------------------------------------
def _make_db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    db.add(Tenant(id=TENANT_ID, name="Aumigao", slug=DEFAULT_TENANT_SLUG, status="active", plan="business"))
    db.add(User(id=WALKER_ID, email="w@sec.com", password_hash="x", role="walker",
                tenant_id=TENANT_ID, full_name="Walker Sec"))
    db.add(User(id=OTHER_WALKER_ID, email="o@sec.com", password_hash="x", role="walker",
                tenant_id=TENANT_ID, full_name="Other"))
    db.add(WalkerProfile(id="wp-sec", user_id=WALKER_ID, full_name="Walker Sec",
                         status="active", active_as_walker=True))
    db.add(WalkerProfile(id="wp-other", user_id=OTHER_WALKER_ID, full_name="Other",
                         status="active", active_as_walker=True))
    db.add(Walk(id=WALK_ID, tutor_id=WALKER_ID, tenant_id=TENANT_ID, walker_id=WALKER_ID,
                pet_id="pet-sec", scheduled_date="2026-06-21", duration_minutes=30,
                price=50.0, operational_status="ride_in_progress"))
    db.commit()
    return db


def _build_app(user_id: str | None = WALKER_ID):
    db = _make_db()
    app = FastAPI()
    app.include_router(pets.router)
    app.include_router(walker.router)
    app.include_router(partner_application.router)
    app.dependency_overrides[get_db] = lambda: db
    if user_id is not None:
        app.dependency_overrides[get_current_user] = lambda: db.get(User, user_id)
    return TestClient(app), db


# Reinicia rate limiter entre testes (singleton de módulo).
@pytest.fixture(autouse=True)
def _reset_upload_rate_limiter():
    uv.upload_rate_limiter._failures.clear()
    yield
    uv.upload_rate_limiter._failures.clear()


# ---------------------------------------------------------------------------
# G3 — extensão inválida → 400
# ---------------------------------------------------------------------------
class TestG3InvalidExtension:
    def test_pets_rejects_exe_extension(self):
        client, _ = _build_app()
        r = client.post(
            "/pets/upload-photo",
            files={"file": ("malware.exe", EXE_BYTES, "application/octet-stream")},
        )
        assert r.status_code == 400, r.text

    def test_pets_rejects_txt_extension(self):
        client, _ = _build_app()
        r = client.post(
            "/pets/upload-photo",
            files={"file": ("notes.txt", b"hello world", "text/plain")},
        )
        assert r.status_code == 400, r.text

    def test_walker_kit_rejects_exe_extension(self):
        client, _ = _build_app()
        r = client.post(
            "/walker/kit/photo",
            files={"file": ("bad.exe", EXE_BYTES, "application/octet-stream")},
        )
        assert r.status_code == 400, r.text

    def test_walker_completion_rejects_txt_extension(self):
        client, _ = _build_app()
        r = client.post(
            f"/walker/walks/{WALK_ID}/completion-photo",
            files={"file": ("readme.txt", b"hello", "text/plain")},
        )
        assert r.status_code == 400, r.text


# ---------------------------------------------------------------------------
# Conteúdo não-imagem (magic bytes errados) → 400
# ---------------------------------------------------------------------------
class TestNonImageContent:
    def test_pets_rejects_exe_magic_bytes(self):
        client, _ = _build_app()
        r = client.post(
            "/pets/upload-photo",
            files={"file": ("photo.jpg", EXE_BYTES, "image/jpeg")},
        )
        assert r.status_code == 400, r.text

    def test_pets_rejects_html_content(self):
        client, _ = _build_app()
        r = client.post(
            "/pets/upload-photo",
            files={"file": ("image.jpg", HTML_BYTES, "image/jpeg")},
        )
        assert r.status_code == 400, r.text

    def test_pets_rejects_svg_content(self):
        client, _ = _build_app()
        r = client.post(
            "/pets/upload-photo",
            files={"file": ("image.svg", SVG_BYTES, "image/jpeg")},
        )
        assert r.status_code == 400, r.text

    def test_walker_kit_rejects_html_magic_bytes(self):
        client, _ = _build_app()
        r = client.post(
            "/walker/kit/photo",
            files={"file": ("photo.jpg", HTML_BYTES, "image/jpeg")},
        )
        assert r.status_code == 400, r.text

    def test_completion_rejects_exe_magic_bytes(self):
        client, _ = _build_app()
        r = client.post(
            f"/walker/walks/{WALK_ID}/completion-photo",
            files={"file": ("photo.jpg", EXE_BYTES, "image/jpeg")},
        )
        assert r.status_code == 400, r.text


# ---------------------------------------------------------------------------
# Imagem válida → aceita (regressão)
# ---------------------------------------------------------------------------
class TestValidImageAccepted:
    def test_pets_accepts_valid_jpeg(self):
        client, _ = _build_app()
        with patch("app.services.object_storage.save"):
            r = client.post(
                "/pets/upload-photo",
                files={"file": ("photo.jpg", JPEG, "image/jpeg")},
            )
        assert r.status_code == 201, r.text

    def test_walker_kit_accepts_valid_jpeg(self):
        client, _ = _build_app()
        with patch("app.services.object_storage.save"):
            r = client.post(
                "/walker/kit/photo",
                files={"file": ("photo.jpg", JPEG, "image/jpeg")},
            )
        assert r.status_code == 200, r.text

    def test_walker_completion_accepts_valid_jpeg(self):
        client, _ = _build_app()
        with patch("app.services.object_storage.save"):
            r = client.post(
                f"/walker/walks/{WALK_ID}/completion-photo",
                files={"file": ("photo.jpg", JPEG, "image/jpeg")},
            )
        assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# G7 — limite de tamanho por tipo
# ---------------------------------------------------------------------------
class TestG7SizeLimits:
    def _make_large_jpeg(self, size_bytes: int) -> bytes:
        """JPEG válido com padding para atingir tamanho alvo."""
        return JPEG + b"\x00" * max(0, size_bytes - len(JPEG))

    def test_pets_rejects_over_5mb(self):
        client, _ = _build_app()
        big_file = self._make_large_jpeg(5 * 1024 * 1024 + 1)
        r = client.post(
            "/pets/upload-photo",
            files={"file": ("big.jpg", big_file, "image/jpeg")},
        )
        assert r.status_code == 413, r.text

    def test_pets_accepts_exactly_5mb(self):
        client, _ = _build_app()
        exactly_5mb = self._make_large_jpeg(5 * 1024 * 1024)
        with patch("app.services.object_storage.save"):
            r = client.post(
                "/pets/upload-photo",
                files={"file": ("ok.jpg", exactly_5mb, "image/jpeg")},
            )
        assert r.status_code == 201, r.text

    def test_walker_kit_rejects_over_5mb(self):
        client, _ = _build_app()
        big_file = self._make_large_jpeg(5 * 1024 * 1024 + 1)
        r = client.post(
            "/walker/kit/photo",
            files={"file": ("big.jpg", big_file, "image/jpeg")},
        )
        assert r.status_code == 413, r.text

    def test_completion_rejects_over_5mb(self):
        client, _ = _build_app()
        big_file = self._make_large_jpeg(5 * 1024 * 1024 + 1)
        r = client.post(
            f"/walker/walks/{WALK_ID}/completion-photo",
            files={"file": ("big.jpg", big_file, "image/jpeg")},
        )
        assert r.status_code == 413, r.text

    def test_partner_doc_accepts_up_to_10mb_pdf(self):
        """Documentos de identidade/endereço têm limite de 10 MB."""
        client, _ = _build_app()
        # Simula um PDF de exatamente 10 MB (passa).
        big_pdf = PDF_BYTES + b"\x00" * (10 * 1024 * 1024 - len(PDF_BYTES))
        with patch("app.services.object_storage.save"):
            r = client.post(
                "/api/partner-applications/uploads",
                data={"document_type": "address_proof", "owner_id": "cand-g7"},
                files={"file": ("proof.pdf", big_pdf, "application/pdf")},
            )
        assert r.status_code == 201, r.text

    def test_partner_doc_rejects_over_10mb(self):
        client, _ = _build_app()
        big_pdf = PDF_BYTES + b"\x00" * (10 * 1024 * 1024 + 1)
        r = client.post(
            "/api/partner-applications/uploads",
            data={"document_type": "address_proof", "owner_id": "cand-g7-big"},
            files={"file": ("proof.pdf", big_pdf, "application/pdf")},
        )
        assert r.status_code == 413, r.text

    def test_partner_photo_rejects_over_5mb(self):
        """Fotos de perfil/selfie têm limite de 5 MB."""
        client, _ = _build_app()
        big_img = self._make_large_jpeg(5 * 1024 * 1024 + 1)
        r = client.post(
            "/api/partner-applications/uploads",
            data={"document_type": "profile_photo", "owner_id": "cand-g7-photo"},
            files={"file": ("photo.jpg", big_img, "image/jpeg")},
        )
        assert r.status_code == 413, r.text


# ---------------------------------------------------------------------------
# G6 — documentos aceitam PDF; selfie/foto rejeitam PDF
# ---------------------------------------------------------------------------
class TestG6DocumentVsPhoto:
    def test_address_proof_accepts_pdf(self):
        client, _ = _build_app()
        with patch("app.services.object_storage.save"):
            r = client.post(
                "/api/partner-applications/uploads",
                data={"document_type": "address_proof", "owner_id": "cand-pdf"},
                files={"file": ("proof.pdf", PDF_BYTES, "application/pdf")},
            )
        assert r.status_code == 201, r.text

    def test_identity_front_accepts_pdf(self):
        client, _ = _build_app()
        with patch("app.services.object_storage.save"):
            r = client.post(
                "/api/partner-applications/uploads",
                data={"document_type": "identity_front", "owner_id": "cand-pdf"},
                files={"file": ("id.pdf", PDF_BYTES, "application/pdf")},
            )
        assert r.status_code == 201, r.text

    def test_identity_back_accepts_pdf(self):
        client, _ = _build_app()
        with patch("app.services.object_storage.save"):
            r = client.post(
                "/api/partner-applications/uploads",
                data={"document_type": "identity_back", "owner_id": "cand-pdf"},
                files={"file": ("id_back.pdf", PDF_BYTES, "application/pdf")},
            )
        assert r.status_code == 201, r.text

    def test_selfie_rejects_pdf(self):
        """Selfie é foto — não deve aceitar PDF."""
        client, _ = _build_app()
        r = client.post(
            "/api/partner-applications/uploads",
            data={"document_type": "selfie", "owner_id": "cand-selfie-pdf"},
            files={"file": ("selfie.pdf", PDF_BYTES, "application/pdf")},
        )
        # Sem content_type image/* → 400 "Envie uma imagem valida."
        assert r.status_code == 400, r.text

    def test_profile_photo_rejects_pdf(self):
        """Foto de perfil é imagem — não aceita PDF."""
        client, _ = _build_app()
        r = client.post(
            "/api/partner-applications/uploads",
            data={"document_type": "profile_photo", "owner_id": "cand-photo-pdf"},
            files={"file": ("profile.pdf", PDF_BYTES, "application/pdf")},
        )
        assert r.status_code == 400, r.text

    def test_address_proof_rejects_exe_content(self):
        """PDF no nome mas EXE nos magic bytes → rejeitado."""
        client, _ = _build_app()
        r = client.post(
            "/api/partner-applications/uploads",
            data={"document_type": "address_proof", "owner_id": "cand-exe"},
            files={"file": ("proof.pdf", EXE_BYTES, "application/pdf")},
        )
        assert r.status_code == 400, r.text


# ---------------------------------------------------------------------------
# U3/G2 — rate limit em E2, E3, E4 → 429
# ---------------------------------------------------------------------------
class TestU3RateLimit:
    """Verifica que enforce_upload_rate_limit é chamado em todos os 3 endpoints.

    Usamos monkeypatch do limiter com max_failures=1 para forçar 429 na 2ª chamada.
    """

    def _fast_limiter(self):
        from app.services.login_rate_limiter import InMemoryLoginRateLimiter
        return InMemoryLoginRateLimiter(max_failures=1, window_seconds=600.0)

    def test_pets_upload_rate_limits_after_n(self):
        client, _ = _build_app()
        limiter = self._fast_limiter()
        uv.upload_rate_limiter._failures.clear()
        # Substitui o limiter do módulo de forma isolada
        original = uv.upload_rate_limiter
        uv.upload_rate_limiter = limiter
        try:
            # 1ª chamada: passa (limiter conta)
            with patch("app.services.object_storage.save"):
                r1 = client.post(
                    "/pets/upload-photo",
                    files={"file": ("p.jpg", JPEG, "image/jpeg")},
                )
            # 2ª chamada: bloqueada
            r2 = client.post(
                "/pets/upload-photo",
                files={"file": ("p.jpg", JPEG, "image/jpeg")},
            )
            assert r2.status_code == 429, r2.text
        finally:
            uv.upload_rate_limiter = original

    def test_walker_kit_rate_limits_after_n(self):
        client, _ = _build_app()
        limiter = self._fast_limiter()
        original = uv.upload_rate_limiter
        uv.upload_rate_limiter = limiter
        try:
            with patch("app.services.object_storage.save"):
                r1 = client.post(
                    "/walker/kit/photo",
                    files={"file": ("p.jpg", JPEG, "image/jpeg")},
                )
            r2 = client.post(
                "/walker/kit/photo",
                files={"file": ("p.jpg", JPEG, "image/jpeg")},
            )
            assert r2.status_code == 429, r2.text
        finally:
            uv.upload_rate_limiter = original

    def test_completion_photo_rate_limits_after_n(self):
        client, _ = _build_app()
        limiter = self._fast_limiter()
        original = uv.upload_rate_limiter
        uv.upload_rate_limiter = limiter
        try:
            with patch("app.services.object_storage.save"):
                r1 = client.post(
                    f"/walker/walks/{WALK_ID}/completion-photo",
                    files={"file": ("p.jpg", JPEG, "image/jpeg")},
                )
            r2 = client.post(
                f"/walker/walks/{WALK_ID}/completion-photo",
                files={"file": ("p.jpg", JPEG, "image/jpeg")},
            )
            assert r2.status_code == 429, r2.text
        finally:
            uv.upload_rate_limiter = original


# ---------------------------------------------------------------------------
# G9 — document_url externa em BackgroundCertificatePayload → 422
# ---------------------------------------------------------------------------
class TestG9DocumentUrl:
    def test_external_url_rejected(self):
        client, _ = _build_app()
        r = client.post(
            "/walker/background/certificate",
            json={
                "cert_type": "pf",
                "cert_number": "12345",
                "document_url": "https://evil.example.com/malware.pdf",
            },
        )
        assert r.status_code == 422, r.text

    def test_http_external_url_rejected(self):
        client, _ = _build_app()
        r = client.post(
            "/walker/background/certificate",
            json={
                "cert_type": "pf",
                "cert_number": "12345",
                "document_url": "http://attacker.io/steal.php",
            },
        )
        assert r.status_code == 422, r.text

    def test_internal_uploads_path_accepted(self):
        """document_url começando com /uploads/ é permitida."""
        client, db = _build_app()
        # Sem background provider configurado → pode retornar 400/404 pelo provider,
        # mas o schema Pydantic não deve rejeitar com 422.
        r = client.post(
            "/walker/background/certificate",
            json={
                "cert_type": "pf",
                "cert_number": "12345",
                "document_url": "/uploads/walker-documents/walker-sec/pf-cert.pdf",
            },
        )
        # 422 seria falha de validação do schema (G9) — qualquer outro código é ok.
        assert r.status_code != 422, f"Schema rejeitou URL interna: {r.text}"

    def test_none_document_url_accepted(self):
        """document_url None (omitido) é válido — campo opcional."""
        client, _ = _build_app()
        r = client.post(
            "/walker/background/certificate",
            json={
                "cert_type": "pf",
                "cert_number": "12345",
                "document_url": None,
            },
        )
        assert r.status_code != 422, f"Rejeição inesperada de None: {r.text}"

    def test_data_uri_rejected(self):
        """data: URI (exfiltração de dados embutida) é rejeitada."""
        client, _ = _build_app()
        r = client.post(
            "/walker/background/certificate",
            json={
                "cert_type": "pf",
                "cert_number": "12345",
                "document_url": "data:application/pdf;base64,JVBERi0xLjQ=",
            },
        )
        assert r.status_code == 422, r.text
