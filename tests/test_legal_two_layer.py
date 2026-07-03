"""Testes do aceite legal em 2 camadas (plataforma + tenant) — Fase legal-bloqueante.

Monta apps FastAPI MINIMOS (SQLite em memoria) com overrides de get_db/get_current_user,
seguindo o padrao de tests/test_routes_legal.py. NAO importa app.main.
"""
import app.models  # noqa: F401 — registra todas as tabelas no Base.metadata
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.dependencies.auth import get_current_user, require_admin
from app.models.tenant import Tenant
from app.models.user import User
from app.routes import legal, admin_legal_documents
from app.routes.legal import LEGAL_VERSION

TENANT_ID = "t-aumigao"
TENANT_NAME = "Aumigao"

import pytest


@pytest.fixture(autouse=True)
def _enable_legal_enforcement(monkeypatch):
    """Liga o enforcement de aceite legal (default OFF na suite legada).
    Esta suite testa o BLOQUEIO, entao precisa da flag ON.
    """
    monkeypatch.setenv("LEGAL_ACCEPTANCE_ENFORCED", "true")


def _engine():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return engine


def build_app_layer(*, role="tutor", tenant_id=TENANT_ID, authenticated=True):
    """App com o router de legal + middleware que injeta request.state.tenant_id."""
    engine = _engine()
    db = sessionmaker(bind=engine)()
    if tenant_id:
        db.add(Tenant(id=tenant_id, name=TENANT_NAME, slug=tenant_id, status="active", plan="enterprise"))
    user_id = f"user-{role}"
    db.add(User(id=user_id, email=f"{role}@t.com", password_hash="x", role=role, tenant_id=tenant_id))
    db.commit()

    app = FastAPI()

    @app.middleware("http")
    async def _tenant_mw(request: Request, call_next):
        request.state.tenant_id = tenant_id
        return await call_next(request)

    app.include_router(legal.router)
    app.dependency_overrides[get_db] = lambda: db
    if authenticated:
        app.dependency_overrides[get_current_user] = lambda: db.get(User, user_id)
    else:
        def _unauth():
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="nao autenticado")
        app.dependency_overrides[get_current_user] = _unauth
    return TestClient(app), db, user_id


def build_admin_app(*, tenant_id=TENANT_ID):
    engine = _engine()
    db = sessionmaker(bind=engine)()
    db.add(Tenant(id=tenant_id, name=TENANT_NAME, slug=tenant_id, status="active", plan="enterprise"))
    admin_id = "admin-1"
    db.add(User(id=admin_id, email="a@t.com", password_hash="x", role="admin", tenant_id=tenant_id))
    db.commit()

    app = FastAPI()
    app.include_router(admin_legal_documents.api_router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[require_admin] = lambda: db.get(User, admin_id)
    return TestClient(app), db, admin_id


# ------------------------- Migration id (guarda <=32) -------------------------
def test_migration_0096_revision_id_fits_32_chars():
    rev = "0096_legal_acceptance_v2"
    assert len(rev) <= 32, f"{rev} = {len(rev)} chars"


# ------------------------------ GET /legal/status ----------------------------
def test_status_no_acceptance_full_pending():
    client, _, _ = build_app_layer(role="tutor")
    r = client.get("/legal/status")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["platform"]["accepted"] is False
    assert len(body["platform"]["pending_types"]) == 5
    assert body["tenant"] is not None
    assert body["tenant"]["tenant_id"] == TENANT_ID
    assert body["tenant"]["tenant_name"] == TENANT_NAME
    assert body["tenant"]["accepted"] is False
    # tutor: service_terms + service_cancellation
    assert set(body["tenant"]["pending_types"]) == {"service_terms", "service_cancellation"}


def test_status_tenant_null_when_no_active_tenant():
    client, _, _ = build_app_layer(role="tutor", tenant_id=None)
    r = client.get("/legal/status")
    assert r.status_code == 200, r.text
    assert r.json()["tenant"] is None


def test_status_passeador_tenant_pending_is_walker_agreement():
    client, _, _ = build_app_layer(role="walker")
    body = client.get("/legal/status").json()
    assert body["tenant"]["pending_types"] == ["walker_agreement"]


# ---------------------- POST /legal/acceptance (platform) --------------------
def test_accept_platform_then_platform_ok():
    client, _, _ = build_app_layer(role="tutor")
    r = client.post("/legal/acceptance", json={"accepted": True, "scope": "platform"})
    assert r.status_code == 200, r.text
    assert r.json()["scope"] == "platform"
    assert r.json()["acceptance"]["tenant_id"] is None
    body = client.get("/legal/status").json()
    assert body["platform"]["accepted"] is True
    # tenant continua pendente (nao foi aceito)
    assert body["tenant"]["accepted"] is False


def test_accept_platform_default_scope_is_platform():
    client, _, _ = build_app_layer(role="tutor")
    r = client.post("/legal/acceptance", json={"accepted": True})
    assert r.status_code == 200, r.text
    assert r.json()["scope"] == "platform"


# ---------------------- POST /legal/acceptance (tenant) ----------------------
def test_accept_tenant_records_tenant_id_and_versions():
    client, db, _ = build_app_layer(role="tutor")
    r = client.post("/legal/acceptance", json={"scope": "tenant", "accepted": True})
    assert r.status_code == 200, r.text
    acc = r.json()["acceptance"]
    assert acc["tenant_id"] == TENANT_ID
    assert acc["terms_version"] == "base-2026-07"
    assert acc["cancellation_version"] == "base-2026-07"
    body = client.get("/legal/status").json()
    assert body["tenant"]["accepted"] is True
    # plataforma ainda pendente
    assert body["platform"]["accepted"] is False


# ------------------- Custom doc muda versao -> re-aceite ----------------------
def test_custom_doc_bumps_version_and_reopens_pending():
    client, db, _ = build_app_layer(role="tutor")
    # aceita tenant no base
    client.post("/legal/acceptance", json={"scope": "tenant", "accepted": True})
    assert client.get("/legal/status").json()["tenant"]["accepted"] is True

    # tenant customiza service_terms -> nova versao vigente (v1)
    from app.services import tenant_legal_document_service as tld
    tld.upsert_custom(db, TENANT_ID, "service_terms", "Novo Titulo", "Novo conteudo custom.")
    db.commit()

    body = client.get("/legal/status").json()
    assert body["tenant"]["accepted"] is False
    assert "service_terms" in body["tenant"]["pending_types"]
    # service_cancellation (nao mexido) NAO deve estar pendente
    assert "service_cancellation" not in body["tenant"]["pending_types"]

    # re-aceita -> registra a versao custom vigente
    client.post("/legal/acceptance", json={"scope": "tenant", "accepted": True})
    assert client.get("/legal/status").json()["tenant"]["accepted"] is True


# ------------------------------ Admin F2 -------------------------------------
def test_admin_get_returns_base_parametrized():
    client, _, _ = build_admin_app()
    r = client.get("/api/admin/legal-documents")
    assert r.status_code == 200, r.text
    docs = {d["doc_type"]: d for d in r.json()["documents"]}
    assert set(docs) == {"service_terms", "service_cancellation", "walker_agreement"}
    st = docs["service_terms"]
    assert st["is_custom"] is False
    assert st["version"] == "base-2026-07"
    assert TENANT_NAME in st["title"]
    assert "sem responsabilidade" in st["content"]


def test_admin_put_creates_v1_then_v2():
    client, _, _ = build_admin_app()
    r1 = client.put("/api/admin/legal-documents/service_terms", json={"title": "T1", "content": "C1"})
    assert r1.status_code == 200, r1.text
    assert r1.json()["is_custom"] is True
    assert r1.json()["version"] == "v1"

    r2 = client.put("/api/admin/legal-documents/service_terms", json={"title": "T2", "content": "C2"})
    assert r2.status_code == 200, r2.text
    assert r2.json()["version"] == "v2"

    # GET reflete a versao custom vigente (v2)
    docs = {d["doc_type"]: d for d in client.get("/api/admin/legal-documents").json()["documents"]}
    assert docs["service_terms"]["is_custom"] is True
    assert docs["service_terms"]["version"] == "v2"
    assert docs["service_terms"]["title"] == "T2"


def test_admin_delete_restores_base():
    client, _, _ = build_admin_app()
    client.put("/api/admin/legal-documents/service_terms", json={"title": "T1", "content": "C1"})
    r = client.delete("/api/admin/legal-documents/service_terms")
    assert r.status_code == 200, r.text
    assert r.json()["is_custom"] is False
    assert r.json()["version"] == "base-2026-07"
    docs = {d["doc_type"]: d for d in client.get("/api/admin/legal-documents").json()["documents"]}
    assert docs["service_terms"]["is_custom"] is False


def test_admin_put_invalid_doc_type_404():
    client, _, _ = build_admin_app()
    r = client.put("/api/admin/legal-documents/nao_existe", json={"title": "x", "content": "y"})
    assert r.status_code == 404


# ------------------------------ Enforcement ----------------------------------
def _enforced_app(*, role="tutor", tenant_id=TENANT_ID):
    """App minimo com um endpoint operacional protegido pela dependency de aceite."""
    engine = _engine()
    db = sessionmaker(bind=engine)()
    if tenant_id:
        db.add(Tenant(id=tenant_id, name=TENANT_NAME, slug=tenant_id, status="active", plan="enterprise"))
    user_id = f"user-{role}"
    db.add(User(id=user_id, email=f"{role}@t.com", password_hash="x", role=role, tenant_id=tenant_id))
    db.commit()

    from app.dependencies.legal_gate import require_legal_acceptance

    app = FastAPI()

    @app.middleware("http")
    async def _mw(request: Request, call_next):
        request.state.tenant_id = tenant_id
        return await call_next(request)

    from fastapi import Depends

    @app.post("/op/do", dependencies=[Depends(require_legal_acceptance())])
    def _op():
        return {"ok": True}

    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: db.get(User, user_id)
    # tambem exponho o router legal para aceitar dentro do mesmo db/app
    app.include_router(legal.router)
    return TestClient(app), db, user_id


def test_enforce_blocks_without_any_acceptance_platform_first():
    client, _, _ = _enforced_app(role="tutor")
    r = client.post("/op/do")
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["code"] == "legal_acceptance_required"
    assert r.json()["detail"]["scope"] == "platform"


def test_enforce_blocks_tenant_when_platform_done():
    client, _, _ = _enforced_app(role="tutor")
    client.post("/legal/acceptance", json={"accepted": True, "scope": "platform"})
    r = client.post("/op/do")
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["scope"] == "tenant"
    assert r.json()["detail"]["tenant_id"] == TENANT_ID


def test_enforce_passes_with_both_layers():
    client, _, _ = _enforced_app(role="tutor")
    client.post("/legal/acceptance", json={"accepted": True, "scope": "platform"})
    client.post("/legal/acceptance", json={"accepted": True, "scope": "tenant"})
    r = client.post("/op/do")
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True


def test_enforce_walker_needs_both_layers():
    client, _, _ = _enforced_app(role="walker")
    # sem aceite -> platform
    assert client.post("/op/do").json()["detail"]["scope"] == "platform"
    client.post("/legal/acceptance", json={"accepted": True, "scope": "platform"})
    # agora tenant (walker_agreement)
    assert client.post("/op/do").json()["detail"]["scope"] == "tenant"
    client.post("/legal/acceptance", json={"accepted": True, "scope": "tenant"})
    assert client.post("/op/do").status_code == 200


def test_enforce_platform_only_when_no_active_tenant():
    client, _, _ = _enforced_app(role="tutor", tenant_id=None)
    client.post("/legal/acceptance", json={"accepted": True, "scope": "platform"})
    # sem tenant ativo, camada tenant nao e exigida
    r = client.post("/op/do")
    assert r.status_code == 200, r.text
