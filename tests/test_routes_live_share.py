from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 — registra tabelas
from app.core.database import Base, get_db
from app.dependencies.auth import get_current_user
from app.models.pet import Pet
from app.models.tenant import Tenant, TenantBranding
from app.models.user import User
from app.models.walk import Walk
from app.models.walk_location_ping import WalkLocationPing
from app.routes import live_share

TENANT_ID = "t1"
TUTOR_ID = "tutor1"
OTHER_ID = "tutor2"
WALKER_ID = "walker1"
PET_ID = "pet1"
WALK_ID = "walk1"


def build(monkeypatch, *, operational_status="ride_in_progress", with_pings=True, with_branding=False):
    monkeypatch.setenv("LIVE_SHARE_ENABLED", "true")
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    db.add(Tenant(id=TENANT_ID, name="Pet Shop X", slug="petx", status="active", plan="business"))
    if with_branding:
        db.add(TenantBranding(
            tenant_id=TENANT_ID,
            display_name="Pet Shop X — White Label",
            logo_url="https://cdn.aumigaowalk.com.br/tenant-branding-images/logo-petx.png",
            primary_color="#123456",
        ))
    # Tenant B — mesmo servidor, branding próprio distinto — prova que o payload
    # de um passeio nunca vaza o branding de outro tenant.
    db.add(Tenant(id="t2", name="Pet Shop Y", slug="pety", status="active", plan="business"))
    db.add(TenantBranding(
        tenant_id="t2",
        display_name="Pet Shop Y — White Label",
        logo_url="https://cdn.aumigaowalk.com.br/tenant-branding-images/logo-pety.png",
        primary_color="#abcdef",
    ))
    db.add(User(id=TUTOR_ID, email="t@t.com", password_hash="x", role="cliente", tenant_id=TENANT_ID))
    db.add(User(id=OTHER_ID, email="o@t.com", password_hash="x", role="cliente", tenant_id=TENANT_ID))
    db.add(User(id=WALKER_ID, email="w@t.com", password_hash="x", role="walker", tenant_id=TENANT_ID))
    db.add(Pet(id=PET_ID, tutor_id=TUTOR_ID, name="Rex do Carmo", tenant_id=TENANT_ID, photo_url="http://img/x.png"))
    db.add(Walk(
        id=WALK_ID, tutor_id=TUTOR_ID, walker_id=WALKER_ID, tenant_id=TENANT_ID, pet_id=PET_ID,
        scheduled_date="2026-06-12T14:00:00", duration_minutes=45, price=50.0,
        status="Passeando agora", operational_status=operational_status, walker_selection_mode="auto",
    ))
    if with_pings:
        t0 = datetime(2026, 6, 12, 14, 5, 0)
        db.add(WalkLocationPing(id="p0", walk_id=WALK_ID, walker_id=WALKER_ID,
                                latitude=0.0, longitude=0.0, recorded_at=t0, created_at=t0))
        db.add(WalkLocationPing(id="p1", walk_id=WALK_ID, walker_id=WALKER_ID,
                                latitude=0.003, longitude=0.0, recorded_at=t0 + timedelta(minutes=1), created_at=t0))
    db.commit()

    monkeypatch.setattr(live_share, "global_scope_session", _fake_scope(db))

    class _Current:
        uid = TUTOR_ID
        def __call__(self):
            return db.get(User, self.uid)
    current = _Current()

    test_app = FastAPI()
    test_app.include_router(live_share.router)
    test_app.dependency_overrides[get_db] = lambda: db
    test_app.dependency_overrides[get_current_user] = current
    return TestClient(test_app, raise_server_exceptions=True), db, current


def _fake_scope(db):
    from contextlib import contextmanager
    @contextmanager
    def _scope():
        yield db
    return _scope


def test_create_share_link_returns_token_and_url(monkeypatch):
    client, db, _ = build(monkeypatch)
    r = client.post(f"/walks/{WALK_ID}/share-link")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["token"]
    assert body["url"].endswith(body["token"])
    assert "app.aumigaowalk.com.br/live/" in body["url"]


def test_create_share_link_idempotent(monkeypatch):
    client, db, _ = build(monkeypatch)
    t1 = client.post(f"/walks/{WALK_ID}/share-link").json()["token"]
    t2 = client.post(f"/walks/{WALK_ID}/share-link").json()["token"]
    assert t1 == t2


def test_create_share_link_forbidden_for_non_owner(monkeypatch):
    client, db, current = build(monkeypatch)
    current.uid = OTHER_ID
    r = client.post(f"/walks/{WALK_ID}/share-link")
    assert r.status_code == 403


def test_public_live_returns_sanitized_payload(monkeypatch):
    client, db, _ = build(monkeypatch)
    token = client.post(f"/walks/{WALK_ID}/share-link").json()["token"]
    r = client.get(f"/public/live/{token}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "active"
    assert body["pet_first_name"] == "Rex"
    assert "Carmo" not in str(body)
    # Sem TenantBranding cadastrado: nome cai no fallback Tenant.name e o
    # logo/cor ficam None — a página do site então usa o logo padrão (correto).
    assert body["tenant"]["name"] == "Pet Shop X"
    assert body["tenant"]["slug"] == "petx"
    assert body["tenant"]["logo_url"] is None
    assert body["tenant"]["primary_color"] is None
    assert body["count"] == 1
    assert body["pings"][0]["latitude"] == 0.003


def test_public_live_uses_tenant_branding_logo_not_legacy_column(monkeypatch):
    """Bug real: a página pública mostrava a logo padrão em vez da logo do
    tenant. Causa: o endpoint lia getattr(tenant, "logo_url", None) — o
    modelo Tenant nem tem essa coluna, então getattr sempre caía no default
    None. A logo/nome/cor de white-label moram em TenantBranding
    (tenant.branding), gravados pelo upload do admin. Este teste trava o
    payload correto: nome de exibição do branding, logo_url do branding
    (absoluta) e cor primária."""
    client, db, _ = build(monkeypatch, with_branding=True)
    token = client.post(f"/walks/{WALK_ID}/share-link").json()["token"]
    r = client.get(f"/public/live/{token}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tenant"]["name"] == "Pet Shop X — White Label"
    assert body["tenant"]["logo_url"] == "https://cdn.aumigaowalk.com.br/tenant-branding-images/logo-petx.png"
    assert body["tenant"]["primary_color"] == "#123456"


def test_public_live_does_not_leak_other_tenant_branding(monkeypatch):
    """O passeio é do tenant t1 (petx) — o payload nunca pode trazer o
    branding do tenant t2 (pety), mesmo que ambos existam no mesmo banco."""
    client, db, _ = build(monkeypatch, with_branding=True)
    token = client.post(f"/walks/{WALK_ID}/share-link").json()["token"]
    r = client.get(f"/public/live/{token}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tenant"]["slug"] == "petx"
    assert "pety" not in str(body)
    assert "logo-pety" not in str(body)
    assert "#abcdef" not in str(body)


def test_public_live_unknown_token_404(monkeypatch):
    client, db, _ = build(monkeypatch)
    r = client.get("/public/live/naoexiste")
    assert r.status_code == 404


def test_public_live_gone_when_revoked(monkeypatch):
    client, db, _ = build(monkeypatch)
    token = client.post(f"/walks/{WALK_ID}/share-link").json()["token"]
    from app.models.walk_share_link import WalkShareLink
    link = db.query(WalkShareLink).filter(WalkShareLink.token == token).first()
    link.revoked_at = datetime.utcnow()
    db.commit()
    r = client.get(f"/public/live/{token}")
    assert r.status_code == 410


def test_public_live_gone_when_walk_ended(monkeypatch):
    client, db, _ = build(monkeypatch, operational_status="ride_completed")
    token = client.post(f"/walks/{WALK_ID}/share-link").json()["token"]
    r = client.get(f"/public/live/{token}")
    assert r.status_code == 410


def test_share_link_gated_off_returns_404(monkeypatch):
    client, db, _ = build(monkeypatch)
    monkeypatch.setenv("LIVE_SHARE_ENABLED", "false")
    r = client.post(f"/walks/{WALK_ID}/share-link")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# R15.3 — CORS na rota publica (bug real: pagina /live/{token} do site, servida
# em app.aumigaowalk.com.br, ficava "Carregando..." pra sempre porque a resposta
# nao trazia Access-Control-Allow-Origin e o fetch do navegador falhava por CORS.
# Usa o app real (app.main) para exercitar o CORSMiddleware de fato — o router
# isolado usado nos testes acima nao tem middleware nenhum.
# ---------------------------------------------------------------------------

def test_public_live_cors_header_present_for_allowed_site_origin():
    from fastapi.testclient import TestClient
    from app.main import app as real_app

    client = TestClient(real_app)
    r = client.get(
        "/api/public/live/naoexiste",
        headers={"Origin": "https://app.aumigaowalk.com.br"},
    )
    assert r.status_code == 404
    assert r.headers.get("access-control-allow-origin") == "https://app.aumigaowalk.com.br"


def test_public_live_cors_header_absent_for_unknown_origin():
    from fastapi.testclient import TestClient
    from app.main import app as real_app

    client = TestClient(real_app)
    r = client.get(
        "/api/public/live/naoexiste",
        headers={"Origin": "https://attacker.example.com"},
    )
    assert r.status_code == 404
    assert "access-control-allow-origin" not in {k.lower() for k in r.headers.keys()}
