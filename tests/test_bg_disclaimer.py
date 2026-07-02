"""BG-disclaimer — testa o campo `disclaimer` em todos os payloads de background check.

Cobre:
- A constante BACKGROUND_CHECK_DISCLAIMER existe em background_check_service.
- GET /walker/background inclui o campo disclaimer.
- POST /walker/background/certificate inclui o campo disclaimer.
- _serialize_walker_profile (admin_serializers) inclui o campo disclaimer
  nos campos de background do perfil serializado (GET /admin/partner-applications).
- PATCH admin /background-certificate inclui disclaimer na resposta.

Padrao: build() minimo em SQLite conforme os demais testes BG-*.
"""
from datetime import datetime
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 — garante registro de todos os modelos
from app.core.database import Base, get_db
from app.dependencies.auth import get_current_user
from app.models.tenant import Tenant, TenantFeature
from app.models.user import User
from app.models.walker_background_certificate import WalkerBackgroundCertificate
from app.models.walker_profile import WalkerProfile
from app.routes import admin as admin_router
from app.routes import walker as walker_router
from app.services.background_check_service import BACKGROUND_CHECK_DISCLAIMER
from app.services.tenant_seed_service import DEFAULT_TENANT_SLUG

# IDs estáveis para os testes
TENANT_ID = "t-disclaimer"
ADMIN_ID = "admin-disclaimer"
WALKER_USER_ID = "walker-disclaimer-user"
WALKER_PROFILE_ID = "walker-disclaimer-profile"


# ---------------------------------------------------------------------------
# Helpers de build
# ---------------------------------------------------------------------------

def _engine_and_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _seed_base(db, *, profile_status: str = "active", bg_status: str = "none"):
    db.add(Tenant(
        id=TENANT_ID, name="Aumigao", slug=DEFAULT_TENANT_SLUG,
        status="active", plan="business",
    ))
    db.add(User(
        id=ADMIN_ID, email="adm@example.com", password_hash="x",
        role="super_admin", full_name="Admin", tenant_id=TENANT_ID,
    ))
    db.add(User(
        id=WALKER_USER_ID, email="walker@example.com", password_hash="x",
        role="walker", full_name="Passeador Disclaimer", tenant_id=TENANT_ID,
    ))
    db.add(WalkerProfile(
        id=WALKER_PROFILE_ID,
        user_id=WALKER_USER_ID,
        full_name="Passeador Disclaimer",
        cpf="11144477735",
        phone="71999990000",
        city="Salvador",
        state="BA",
        status=profile_status,
        active_as_walker=True,
        background_check_status=bg_status,
        created_at=datetime.utcnow(),
    ))
    db.commit()


def _build_walker_client(db):
    """Cliente autenticado como walker."""
    app = FastAPI()
    app.include_router(walker_router.router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: db.get(User, WALKER_USER_ID)
    return TestClient(app)


def _build_admin_client(db):
    """Cliente autenticado como super_admin."""
    app = FastAPI()
    app.include_router(admin_router.router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: db.get(User, ADMIN_ID)
    return TestClient(app)


def _add_cert(db, cert_type: str, status: str, uf: str | None = None) -> WalkerBackgroundCertificate:
    cert = WalkerBackgroundCertificate(
        id=str(uuid4()),
        walker_profile_id=WALKER_PROFILE_ID,
        cert_type=cert_type,
        issuer_uf=uf,
        cert_number=f"{cert_type}-test-1",
        status=status,
    )
    db.add(cert)
    db.commit()
    return cert


# ---------------------------------------------------------------------------
# Testa a constante
# ---------------------------------------------------------------------------

def test_disclaimer_constante_existe_e_nao_vazia():
    """BACKGROUND_CHECK_DISCLAIMER deve ser uma string não vazia."""
    assert isinstance(BACKGROUND_CHECK_DISCLAIMER, str)
    assert len(BACKGROUND_CHECK_DISCLAIMER) > 20


def test_disclaimer_menciona_isenção_de_responsabilidade():
    """O texto deve mencionar que a verificacao nao transfere responsabilidade."""
    texto = BACKGROUND_CHECK_DISCLAIMER.lower()
    assert "responsabilidade" in texto


# ---------------------------------------------------------------------------
# GET /walker/background — inclui disclaimer
# ---------------------------------------------------------------------------

def test_get_background_inclui_disclaimer():
    db = _engine_and_session()
    _seed_base(db)
    # Registra consentimento e envia uma certidão
    client = _build_walker_client(db)
    client.post("/walker/background/consent", json={"consent_version": "v1"})
    client.post("/walker/background/certificate", json={"cert_type": "pf", "cert_number": "PF-1"})

    r = client.get("/walker/background")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "disclaimer" in body, "campo 'disclaimer' ausente no GET /walker/background"
    assert body["disclaimer"] == BACKGROUND_CHECK_DISCLAIMER


def test_get_background_sem_certs_tambem_inclui_disclaimer():
    """Mesmo sem certidões, o disclaimer deve aparecer."""
    db = _engine_and_session()
    _seed_base(db)
    client = _build_walker_client(db)
    client.post("/walker/background/consent", json={})

    r = client.get("/walker/background")
    assert r.status_code == 200, r.text
    assert r.json()["disclaimer"] == BACKGROUND_CHECK_DISCLAIMER


# ---------------------------------------------------------------------------
# POST /walker/background/certificate — inclui disclaimer
# ---------------------------------------------------------------------------

def test_submit_certificate_inclui_disclaimer():
    db = _engine_and_session()
    _seed_base(db)
    client = _build_walker_client(db)
    client.post("/walker/background/consent", json={})

    r = client.post("/walker/background/certificate", json={"cert_type": "pf", "cert_number": "PF-2"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert "disclaimer" in body, "campo 'disclaimer' ausente no POST /walker/background/certificate"
    assert body["disclaimer"] == BACKGROUND_CHECK_DISCLAIMER


# ---------------------------------------------------------------------------
# GET /admin/partner-applications/{id} — inclui disclaimer nos campos de background
# ---------------------------------------------------------------------------

def test_admin_serialize_walker_inclui_disclaimer():
    db = _engine_and_session()
    _seed_base(db)
    _add_cert(db, "pf", "pending")

    client = _build_admin_client(db)
    r = client.get(f"/admin/partner-applications/{WALKER_PROFILE_ID}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "background_disclaimer" in body, (
        "campo 'background_disclaimer' ausente no payload admin do perfil"
    )
    assert body["background_disclaimer"] == BACKGROUND_CHECK_DISCLAIMER


def test_admin_serialize_walker_sem_certs_inclui_disclaimer():
    """Perfil sem certidões ainda deve carregar o disclaimer."""
    db = _engine_and_session()
    _seed_base(db)

    client = _build_admin_client(db)
    r = client.get(f"/admin/partner-applications/{WALKER_PROFILE_ID}")
    assert r.status_code == 200, r.text
    assert r.json()["background_disclaimer"] == BACKGROUND_CHECK_DISCLAIMER


# ---------------------------------------------------------------------------
# PATCH /admin/partner-applications/{id}/background-certificate/{cert_id}
# — inclui disclaimer na resposta
# ---------------------------------------------------------------------------

def test_patch_certificate_resposta_inclui_disclaimer():
    db = _engine_and_session()
    _seed_base(db)
    cert = _add_cert(db, "pf", "pending")

    client = _build_admin_client(db)
    r = client.patch(
        f"/admin/partner-applications/{WALKER_PROFILE_ID}/background-certificate/{cert.id}",
        json={"status": "validated"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    # O PATCH retorna o perfil serializado via _serialize_walker_profile
    assert "background_disclaimer" in body, (
        "campo 'background_disclaimer' ausente na resposta do PATCH de certidao"
    )
    assert body["background_disclaimer"] == BACKGROUND_CHECK_DISCLAIMER
