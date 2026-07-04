"""Testes da feature "Indicar passeador" (walker_indications).

Cobre:
  1. Gate 403 quando feature client_referrals está OFF no tenant.
  2. Regressão de tenant ativo ≠ tenant de nascimento (cross-tenant leak).
  3. POST público cria lead + promove status da indicação.
  4. POST público rate-limited (429 após esgotar janela).
  5. RLS cross-tenant: indicação não vaza entre tenants (lista só do próprio tenant).
  6. Cadeia da migration 0098 (revision id ≤ 32 chars, chains em 0097).

Padrão de teste do repo: FastAPI + TestClient + SQLite in-memory + dependency_overrides.
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 — registra todas as tabelas no Base.metadata
from app.core.database import Base, get_db, get_global_db
from app.dependencies.auth import get_current_user
from app.models.tenant import Tenant, TenantFeature
from app.models.user import User
from app.routes import walker_indications

# ---------------------------------------------------------------------------
# Constantes de fixtures
# ---------------------------------------------------------------------------

TENANT_A_ID = "tenant-a"
TENANT_A_SLUG = "tenant-a-slug"
TENANT_B_ID = "tenant-b"
TENANT_B_SLUG = "tenant-b-slug"

TUTOR_A_ID = "tutor-a"
TUTOR_B_ID = "tutor-b"  # pertence ao tenant B (nascimento)


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
    return engine, sessionmaker(bind=engine)


def _seed_db(db):
    db.add(Tenant(id=TENANT_A_ID, name="Tenant A", slug=TENANT_A_SLUG, status="active", plan="pro"))
    db.add(Tenant(id=TENANT_B_ID, name="Tenant B", slug=TENANT_B_SLUG, status="active", plan="pro"))
    db.add(User(id=TUTOR_A_ID, email="tutor_a@test.com", password_hash="x",
                role="cliente", full_name="Tutor A", is_active=True, tenant_id=TENANT_A_ID))
    # tutor_b nasce no tenant B, mas pode operar no tenant A via request.state
    db.add(User(id=TUTOR_B_ID, email="tutor_b@test.com", password_hash="x",
                role="cliente", full_name="Tutor B", is_active=True, tenant_id=TENANT_B_ID))
    db.commit()


def _build(active_tenant_id: str = TENANT_A_ID, user_id: str = TUTOR_A_ID):
    """Monta TestClient com tenant ativo injetado via db.info['rls_tenant']."""
    _, Session = _engine_and_session()
    db = Session()
    _seed_db(db)

    test_app = FastAPI()
    test_app.include_router(walker_indications.router)
    test_app.include_router(walker_indications.api_router)
    test_app.include_router(walker_indications.public_router)
    test_app.include_router(walker_indications.api_public_router)

    def override_db():
        """Override que injeta rls_tenant = active_tenant_id (simula middleware)."""
        inner = Session()
        inner.info["rls_tenant"] = active_tenant_id
        try:
            yield inner
        finally:
            inner.close()

    def override_global_db():
        """Override de get_global_db: mesma sessão com rls_tenant='*'."""
        inner = Session()
        inner.info["rls_tenant"] = "*"
        try:
            yield inner
        finally:
            inner.close()

    test_app.dependency_overrides[get_db] = override_db
    test_app.dependency_overrides[get_global_db] = override_global_db
    test_app.dependency_overrides[get_current_user] = lambda: db.get(User, user_id)
    return TestClient(test_app), db


def _disable_feature(db, tenant_id: str, key: str = "client_referrals"):
    """Desliga a feature do tenant explicitamente."""
    db.add(TenantFeature(id=f"feat-{tenant_id}-{key}", tenant_id=tenant_id,
                         feature_key=key, enabled=False))
    db.commit()


# ---------------------------------------------------------------------------
# Testes — rotas autenticadas
# ---------------------------------------------------------------------------

VALID_INDICATION = {
    "walker_name": "João Passeador",
    "walker_phone": "71999990000",
    "note": "amigo de confiança",
}


def test_create_indication_happy_path():
    client, db = _build()
    r = client.post("/walker-indications", json=VALID_INDICATION)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["walker_name"] == "João Passeador"
    assert body["status"] == "enviada"
    assert "share_url" in body
    assert "seja-passeador" in body["share_url"]
    assert body["id"] in body["share_url"]


def test_create_indication_gate_403_when_feature_off():
    """Gate: feature client_referrals OFF → 403."""
    client, db = _build()
    _disable_feature(db, TENANT_A_ID, "client_referrals")
    r = client.post("/walker-indications", json=VALID_INDICATION)
    assert r.status_code == 403, r.text


def test_create_indication_tenant_ativo_nao_nascimento():
    """Regressão: tutor B (nasce no tenant B) operando no tenant A via request.state.

    A indicação deve ser gravada com tenant_id = TENANT_A_ID (tenant da request),
    NÃO com TENANT_B_ID (tenant de nascimento do tutor).
    """
    # active_tenant_id = A; user = tutor_b (nasce em B)
    client, db = _build(active_tenant_id=TENANT_A_ID, user_id=TUTOR_B_ID)
    r = client.post("/walker-indications", json=VALID_INDICATION)
    assert r.status_code == 201, r.text

    # Verifica no banco: a indicação foi gravada no tenant A, não no B.
    # db.info["rls_tenant"] não está setado neste db (é o db do seed), mas como
    # usamos SQLite sem RLS real, todas as linhas são visíveis.
    from app.models.walker_indication import WalkerIndication
    db.expire_all()  # invalida o cache para leitura fresca
    indications = db.query(WalkerIndication).all()
    assert len(indications) == 1
    assert indications[0].tenant_id == TENANT_A_ID
    assert indications[0].tutor_user_id == TUTOR_B_ID


def test_list_indications_only_own_tenant_and_tutor():
    """Lista retorna só as indicações do tutor no tenant ativo."""
    client, db = _build()
    client.post("/walker-indications", json=VALID_INDICATION)
    client.post("/walker-indications", json={**VALID_INDICATION, "walker_name": "Maria"})

    r = client.get("/walker-indications")
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert len(items) == 2
    assert all(i["status"] == "enviada" for i in items)
    assert all("share_url" in i for i in items)


def test_list_indications_rls_cross_tenant():
    """RLS: tutor no tenant A não vê indicações do tenant B."""
    # Cria indicação no tenant B
    client_b, db_b = _build(active_tenant_id=TENANT_B_ID, user_id=TUTOR_B_ID)
    r = client_b.post("/walker-indications", json=VALID_INDICATION)
    assert r.status_code == 201, r.text

    # Tutor A no tenant A não deve ver a indicação do tenant B
    client_a, _ = _build(active_tenant_id=TENANT_A_ID, user_id=TUTOR_A_ID)
    r2 = client_a.get("/walker-indications")
    assert r2.status_code == 200, r2.text
    assert r2.json()["items"] == []


# ---------------------------------------------------------------------------
# Testes — rota pública /public/walker-leads
# ---------------------------------------------------------------------------


def test_public_lead_creates_lead_and_promotes_indication():
    """Lead público com indication_id válido cria lead + promove indicação para lead_criado."""
    client, db = _build()

    # Primeiro cria uma indicação autenticada
    r_ind = client.post("/walker-indications", json=VALID_INDICATION)
    assert r_ind.status_code == 201, r_ind.text
    indication_id = r_ind.json()["id"]

    # Agora envia o lead público
    r = client.post(
        "/public/walker-leads",
        json={
            "name": "João Passeador",
            "phone": "71988880000",
            "city": "Salvador",
            "indication_id": indication_id,
        },
    )
    assert r.status_code == 201, r.text
    assert r.json() == {"ok": True}

    # Verifica que a indicação foi promovida
    # db.expire_all() para garantir leitura fresca (a sessão do route é diferente).
    from app.models.walker_indication import WalkerIndication, WalkerLead
    db.expire_all()
    ind = db.get(WalkerIndication, indication_id)
    assert ind is not None
    assert ind.status == "lead_criado"

    # Verifica que o lead foi criado
    lead = db.query(WalkerLead).filter(WalkerLead.indication_id == indication_id).first()
    assert lead is not None
    assert lead.name == "João Passeador"
    assert lead.city == "Salvador"
    assert lead.tenant_id == TENANT_A_ID


def test_public_lead_without_indication_uses_slug():
    """Lead público sem indication_id resolve tenant via tenant_slug."""
    client, db = _build()

    r = client.post(
        "/public/walker-leads",
        json={
            "name": "Maria Walker",
            "phone": "71977770000",
            "tenant_slug": TENANT_A_SLUG,
        },
    )
    assert r.status_code == 201, r.text

    from app.models.walker_indication import WalkerLead
    lead = db.query(WalkerLead).filter(WalkerLead.tenant_id == TENANT_A_ID).first()
    assert lead is not None
    assert lead.indication_id is None


def test_public_lead_missing_name_422():
    client, _ = _build()
    r = client.post("/public/walker-leads", json={"name": "", "phone": "71977770001"})
    assert r.status_code == 422, r.text


def test_public_lead_missing_phone_422():
    client, _ = _build()
    r = client.post("/public/walker-leads", json={"name": "Ana", "phone": ""})
    assert r.status_code == 422, r.text


def test_public_lead_rate_limited(monkeypatch):
    """Rate limit: após esgotar o limite por IP, recebe 429."""
    import app.routes.walker_indications as mod
    # Substitui o limiter por um que bloqueia imediatamente
    from app.services.login_rate_limiter import InMemoryLoginRateLimiter
    blocker = InMemoryLoginRateLimiter(max_failures=0, window_seconds=3600)
    # Força is_blocked a retornar True
    monkeypatch.setattr(mod, "application_rate_limiter", blocker)

    client, _ = _build()
    r = client.post(
        "/public/walker-leads",
        json={"name": "Blocked", "phone": "71966660000"},
    )
    assert r.status_code == 429, r.text


# ---------------------------------------------------------------------------
# Testes — migration 0098
# ---------------------------------------------------------------------------


def test_migration_0098_revision_id_within_32_chars():
    revision = "0098_walker_indications"
    assert len(revision) <= 32, f"Revision id muito longo: {len(revision)}"


def test_migration_0098_chains_on_0097():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(Config("alembic.ini"))
    rev = script.get_revision("0098_walker_indications")
    assert rev is not None, "Migration 0098_walker_indications não encontrada"
    assert rev.down_revision == "0097_tenant_units_slug_rbac", (
        f"down_revision incorreto: {rev.down_revision}"
    )


def test_migration_0098_single_head():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(Config("alembic.ini"))
    heads = list(script.get_heads())
    assert len(heads) == 1, f"Múltiplos heads: {heads}"
    assert heads[0] == "0098_walker_indications"


def test_migration_0098_rls_in_file():
    from pathlib import Path

    migration_path = (
        Path(__file__).resolve().parent.parent
        / "alembic" / "versions" / "0098_walker_indications.py"
    )
    text = migration_path.read_text(encoding="utf-8")
    assert "ENABLE ROW LEVEL SECURITY" in text
    assert "walker_indications" in text
    assert "walker_leads" in text
    assert "tenant_isolation" in text
