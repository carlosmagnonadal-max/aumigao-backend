"""Testes — Fase 1 Passo 3: Passeador Multi-Tenant.

Cobre:
  1. serialize_operational_walk inclui tenant_id, tenant_name, tenant_brand_color.
  2. GET /walker/walks — cada item tem as 3 chaves de tenant.
  3. GET /walker/requests — cada item tem as 3 chaves de tenant.
  4. GET /walker/tenants — retorna só tenants com status=active do walker;
     inclui slug/display_name/brand_color/logo_url/access_status/access_type;
     NÃO retorna pending/declined; NÃO vaza acessos de outro walker.
  5. GET /walker/tenants exige role walker (guard _require_active_walker).

Padrão: FastAPI mínimo, SQLite StaticPool, dependency_overrides.
Segue estilo de test_multitenant_walker_phase1_step1.py.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 — registra todas as tabelas no Base.metadata
from app.core.database import Base, get_db, get_walker_self_db
from app.dependencies.auth import get_current_user
from app.models.tenant import Tenant, TenantBranding
from app.models.tenant_walker_access import TenantWalkerAccess
from app.models.user import User
from app.models.walk import Walk
from app.models.walker_profile import WalkerProfile
from app.services.operational_matching_service import serialize_operational_walk

# ─── IDs de fixture ───────────────────────────────────────────────────────────

WALKER_ID = "walker-step3"
WALKER_B_ID = "walker-step3-b"
TUTOR_ID = "tutor-step3"
TENANT_A_ID = "tenant-step3-a"
TENANT_B_ID = "tenant-step3-b"
PET_ID = "pet-step3"
WALK_WITH_TENANT_ID = "walk-step3-with-tenant"
WALK_NO_TENANT_ID = "walk-step3-no-tenant"

_EXPIRES_AT = datetime.utcnow() + timedelta(minutes=15)


# ─── Banco em memória — cada teste cria o seu (StaticPool isolado) ────────────


def _build_db():
    """Cria banco SQLite isolado em memória para um teste."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    return db


def _populate(db):
    """Popula banco com tenants (um com branding, um sem), walker, walks."""
    from app.models.pet import Pet

    # Tenant A — com branding
    db.add(Tenant(id=TENANT_A_ID, name="Rede A", slug="rede-a", status="active", plan="business"))
    db.add(TenantBranding(
        id="brand-a",
        tenant_id=TENANT_A_ID,
        display_name="Rede Animal A",
        primary_color="#FF5500",
        logo_url="https://cdn.example.com/logo-a.png",
    ))

    # Tenant B — sem branding
    db.add(Tenant(id=TENANT_B_ID, name="Rede B", slug="rede-b", status="active", plan="starter"))

    # Usuários
    db.add(User(id=TUTOR_ID, email="tutor-s3@test.com", password_hash="x", role="tutor", tenant_id=TENANT_A_ID))
    db.add(User(id=WALKER_ID, email="walker-s3@test.com", password_hash="x", role="walker", tenant_id=TENANT_A_ID))
    db.add(User(id=WALKER_B_ID, email="walker-s3b@test.com", password_hash="x", role="walker", tenant_id=TENANT_A_ID))

    # WalkerProfile — ativo
    db.add(WalkerProfile(
        id="wp-step3",
        user_id=WALKER_ID,
        status="active",
        active_as_walker=True,
    ))
    # WalkerProfile walker B — ativo
    db.add(WalkerProfile(
        id="wp-step3-b",
        user_id=WALKER_B_ID,
        status="active",
        active_as_walker=True,
    ))

    # Pet
    db.add(Pet(id=PET_ID, tutor_id=TUTOR_ID, tenant_id=TENANT_A_ID, name="Bob"))

    # Walk com tenant
    db.add(Walk(
        id=WALK_WITH_TENANT_ID,
        tutor_id=TUTOR_ID,
        pet_id=PET_ID,
        tenant_id=TENANT_A_ID,
        walker_id=WALKER_ID,
        scheduled_date="2026-07-01T10:00",
        duration_minutes=30,
        price=50.0,
        status="Concluído",
        operational_status="ride_completed",
    ))

    # Walk sem tenant (tenant_id=None)
    db.add(Walk(
        id=WALK_NO_TENANT_ID,
        tutor_id=TUTOR_ID,
        pet_id=PET_ID,
        tenant_id=None,
        walker_id=WALKER_ID,
        scheduled_date="2026-07-02T10:00",
        duration_minutes=30,
        price=40.0,
        status="Concluído",
        operational_status="ride_completed",
    ))

    # TenantWalkerAccess para walker A
    db.add(TenantWalkerAccess(
        id="twa-step3-active",
        tenant_id=TENANT_A_ID,
        walker_user_id=WALKER_ID,
        status="active",
        access_type="shared_network",
    ))
    # Walker A tem acesso pendente em Tenant B (usa id distinto do active)
    db.add(TenantWalkerAccess(
        id="twa-step3-pending",
        tenant_id=TENANT_B_ID,
        walker_user_id=WALKER_ID,
        status="pending",
        access_type="shared_network",
    ))

    # TenantWalkerAccess para walker B — active em A (não deve aparecer para walker A)
    db.add(TenantWalkerAccess(
        id="twa-step3-b-active",
        tenant_id=TENANT_A_ID,
        walker_user_id=WALKER_B_ID,
        status="active",
        access_type="tenant_exclusive",
    ))

    db.commit()


# ─── Fixture de app mínimo com overrides ─────────────────────────────────────


def _make_app_and_client(db):
    """Cria FastAPI mínimo com walker router e dependency overrides."""
    from app.routes import walker as walker_module

    app = FastAPI()
    app.include_router(walker_module.router)

    walker_user = db.get(User, WALKER_ID)

    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_walker_self_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: walker_user

    return TestClient(app, raise_server_exceptions=True)


def _make_app_non_walker(db):
    """App com usuário tutor (não-walker) para testar guard."""
    from app.routes import walker as walker_module

    app = FastAPI()
    app.include_router(walker_module.router)

    tutor_user = db.get(User, TUTOR_ID)

    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_walker_self_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: tutor_user

    return TestClient(app, raise_server_exceptions=False)


# ═══════════════════════════════════════════════════════════════════════════════
# 1. serialize_operational_walk — 3 campos de tenant
# ═══════════════════════════════════════════════════════════════════════════════


def test_serialize_walk_com_tenant_e_branding():
    """Walk com tenant que tem branding: tenant_name=display_name, brand_color=primary_color."""
    db = _build_db()
    _populate(db)

    walk = db.get(Walk, WALK_WITH_TENANT_ID)
    result = serialize_operational_walk(walk, db, user=None)

    assert result["tenant_id"] == TENANT_A_ID
    assert result["tenant_name"] == "Rede Animal A"    # display_name do branding
    assert result["tenant_brand_color"] == "#FF5500"   # primary_color do branding


def test_serialize_walk_sem_branding_usa_tenant_name():
    """Walk com tenant sem branding: tenant_name cai para Tenant.name, brand_color=None."""
    db = _build_db()
    _populate(db)

    # Cria walk em tenant B (sem branding)
    walk_b = Walk(
        id="walk-step3-b",
        tutor_id=TUTOR_ID,
        pet_id=PET_ID,
        tenant_id=TENANT_B_ID,
        walker_id=WALKER_ID,
        scheduled_date="2026-07-03T10:00",
        duration_minutes=30,
        price=30.0,
        status="Concluído",
        operational_status="ride_completed",
    )
    db.add(walk_b)
    db.commit()
    db.refresh(walk_b)

    result = serialize_operational_walk(walk_b, db, user=None)

    assert result["tenant_id"] == TENANT_B_ID
    assert result["tenant_name"] == "Rede B"   # fallback: Tenant.name
    assert result["tenant_brand_color"] is None


def test_serialize_walk_sem_tenant_todos_campos_none():
    """Walk sem tenant (tenant_id=None): todos os 3 campos de tenant são None."""
    db = _build_db()
    _populate(db)

    walk = db.get(Walk, WALK_NO_TENANT_ID)
    result = serialize_operational_walk(walk, db, user=None)

    assert result["tenant_id"] is None
    assert result["tenant_name"] is None
    assert result["tenant_brand_color"] is None


def test_serialize_walk_tem_as_3_chaves_de_tenant():
    """serialize_operational_walk sempre inclui as 3 chaves, mesmo se None."""
    db = _build_db()
    _populate(db)

    walk = db.get(Walk, WALK_NO_TENANT_ID)
    result = serialize_operational_walk(walk, db, user=None)

    assert "tenant_id" in result
    assert "tenant_name" in result
    assert "tenant_brand_color" in result


# ═══════════════════════════════════════════════════════════════════════════════
# 2. GET /walker/walks — cada item tem as 3 chaves
# ═══════════════════════════════════════════════════════════════════════════════


def test_walker_walks_inclui_tenant_fields():
    """GET /walker/walks: cada item tem tenant_id, tenant_name, tenant_brand_color."""
    db = _build_db()
    _populate(db)
    client = _make_app_and_client(db)

    resp = client.get("/walker/walks")
    assert resp.status_code == 200
    walks = resp.json()
    assert len(walks) >= 1

    for item in walks:
        assert "tenant_id" in item, f"tenant_id ausente em {item.get('id')}"
        assert "tenant_name" in item, f"tenant_name ausente em {item.get('id')}"
        assert "tenant_brand_color" in item, f"tenant_brand_color ausente em {item.get('id')}"


def test_walker_walks_tenant_com_branding_tem_valores_corretos():
    """GET /walker/walks: walk com tenant A tem display_name e primary_color corretos."""
    db = _build_db()
    _populate(db)
    client = _make_app_and_client(db)

    resp = client.get("/walker/walks")
    assert resp.status_code == 200
    walks = resp.json()

    walk_a = next((w for w in walks if w["id"] == WALK_WITH_TENANT_ID), None)
    assert walk_a is not None
    assert walk_a["tenant_id"] == TENANT_A_ID
    assert walk_a["tenant_name"] == "Rede Animal A"
    assert walk_a["tenant_brand_color"] == "#FF5500"


def test_walker_walks_sem_tenant_campos_sao_none():
    """GET /walker/walks: walk sem tenant tem tenant_id=None e campos derivados None."""
    db = _build_db()
    _populate(db)
    client = _make_app_and_client(db)

    resp = client.get("/walker/walks")
    assert resp.status_code == 200
    walks = resp.json()

    walk_no = next((w for w in walks if w["id"] == WALK_NO_TENANT_ID), None)
    assert walk_no is not None
    assert walk_no["tenant_id"] is None
    assert walk_no["tenant_name"] is None
    assert walk_no["tenant_brand_color"] is None


# ═══════════════════════════════════════════════════════════════════════════════
# 3. GET /walker/requests — cada item tem as 3 chaves (via payload.update)
# ═══════════════════════════════════════════════════════════════════════════════


def test_walker_requests_inclui_tenant_fields():
    """GET /walker/requests: payload.update propaga as 3 chaves de tenant."""
    from app.models.walk import WalkMatchingAttempt

    db = _build_db()
    _populate(db)

    # Cria walk em estado pending_walker_confirmation com attempt pendente
    req_walk = Walk(
        id="walk-step3-req",
        tutor_id=TUTOR_ID,
        pet_id=PET_ID,
        tenant_id=TENANT_A_ID,
        walker_id=WALKER_ID,
        assigned_walker_id=WALKER_ID,
        scheduled_date="2026-07-10T10:00",
        duration_minutes=30,
        price=50.0,
        status="Aguardando",
        operational_status="pending_walker_confirmation",
    )
    db.add(req_walk)
    db.flush()

    attempt = WalkMatchingAttempt(
        id="attempt-step3",
        walk_id="walk-step3-req",
        walker_id=WALKER_ID,
        status="pending",
        attempt_number=1,
        expires_at=_EXPIRES_AT,
    )
    db.add(attempt)
    db.commit()

    client = _make_app_and_client(db)
    resp = client.get("/walker/requests")
    assert resp.status_code == 200

    items = resp.json()
    assert len(items) >= 1

    for item in items:
        assert "tenant_id" in item, f"tenant_id ausente em /requests item {item.get('id')}"
        assert "tenant_name" in item, f"tenant_name ausente em /requests item {item.get('id')}"
        assert "tenant_brand_color" in item, f"tenant_brand_color ausente em /requests item {item.get('id')}"


def test_walker_requests_tenant_valores_corretos():
    """GET /walker/requests: item com tenant A tem os valores corretos de branding."""
    from app.models.walk import WalkMatchingAttempt

    db = _build_db()
    _populate(db)

    req_walk = Walk(
        id="walk-step3-req2",
        tutor_id=TUTOR_ID,
        pet_id=PET_ID,
        tenant_id=TENANT_A_ID,
        walker_id=WALKER_ID,
        assigned_walker_id=WALKER_ID,
        scheduled_date="2026-07-11T10:00",
        duration_minutes=30,
        price=55.0,
        status="Aguardando",
        operational_status="pending_walker_confirmation",
    )
    db.add(req_walk)
    db.flush()
    db.add(WalkMatchingAttempt(
        id="attempt-step3-2",
        walk_id="walk-step3-req2",
        walker_id=WALKER_ID,
        status="pending",
        attempt_number=1,
        expires_at=_EXPIRES_AT,
    ))
    db.commit()

    client = _make_app_and_client(db)
    resp = client.get("/walker/requests")
    assert resp.status_code == 200

    items = resp.json()
    item = next((i for i in items if i["id"] == "walk-step3-req2"), None)
    assert item is not None
    assert item["tenant_id"] == TENANT_A_ID
    assert item["tenant_name"] == "Rede Animal A"
    assert item["tenant_brand_color"] == "#FF5500"


# ═══════════════════════════════════════════════════════════════════════════════
# 4. GET /walker/tenants — filtragem e estrutura
# ═══════════════════════════════════════════════════════════════════════════════


def test_walker_tenants_retorna_so_active():
    """GET /walker/tenants: apenas acessos status=active aparecem (pending excluído)."""
    db = _build_db()
    _populate(db)
    client = _make_app_and_client(db)

    resp = client.get("/walker/tenants")
    assert resp.status_code == 200

    tenants = resp.json()
    # Walker A tem active em TENANT_A e pending em TENANT_B
    tenant_ids = [t["tenant_id"] for t in tenants]
    assert TENANT_A_ID in tenant_ids, "tenant ativo deve aparecer"
    assert TENANT_B_ID not in tenant_ids, "tenant pendente NÃO deve aparecer"


def test_walker_tenants_estrutura_de_campos():
    """GET /walker/tenants: item tem todos os campos esperados."""
    db = _build_db()
    _populate(db)
    client = _make_app_and_client(db)

    resp = client.get("/walker/tenants")
    assert resp.status_code == 200

    tenants = resp.json()
    assert len(tenants) >= 1

    item = tenants[0]
    assert "tenant_id" in item
    assert "slug" in item
    assert "display_name" in item
    assert "brand_color" in item
    assert "logo_url" in item
    assert "access_status" in item
    assert "access_type" in item


def test_walker_tenants_display_name_usa_branding():
    """GET /walker/tenants: display_name usa TenantBranding.display_name quando disponível."""
    db = _build_db()
    _populate(db)
    client = _make_app_and_client(db)

    resp = client.get("/walker/tenants")
    assert resp.status_code == 200

    item = next(t for t in resp.json() if t["tenant_id"] == TENANT_A_ID)
    assert item["display_name"] == "Rede Animal A"
    assert item["brand_color"] == "#FF5500"
    assert item["logo_url"] == "https://cdn.example.com/logo-a.png"
    assert item["slug"] == "rede-a"
    assert item["access_status"] == "active"
    assert item["access_type"] == "shared_network"


def test_walker_tenants_sem_branding_usa_tenant_name():
    """GET /walker/tenants: tenant sem branding → display_name=Tenant.name, brand_color=None."""
    from app.models.pet import Pet

    db = _build_db()
    # Banco mínimo: apenas walker A com acesso ACTIVE a Tenant B (sem branding)
    db.add(Tenant(id=TENANT_B_ID, name="Rede B", slug="rede-b", status="active", plan="starter"))
    db.add(User(id=WALKER_ID, email="walker-s3@test.com", password_hash="x", role="walker", tenant_id=TENANT_B_ID))
    db.add(WalkerProfile(id="wp-step3-nb", user_id=WALKER_ID, status="active", active_as_walker=True))
    db.add(TenantWalkerAccess(
        id="twa-nb-active",
        tenant_id=TENANT_B_ID,
        walker_user_id=WALKER_ID,
        status="active",
        access_type="shared_network",
    ))
    db.commit()

    client = _make_app_and_client(db)
    resp = client.get("/walker/tenants")
    assert resp.status_code == 200

    item = next((t for t in resp.json() if t["tenant_id"] == TENANT_B_ID), None)
    assert item is not None
    assert item["display_name"] == "Rede B"   # Tenant.name como fallback
    assert item["brand_color"] is None


def test_walker_tenants_nao_retorna_acessos_de_outro_walker():
    """GET /walker/tenants: walker A não vê acessos do walker B."""
    db = _build_db()
    _populate(db)
    client = _make_app_and_client(db)

    resp = client.get("/walker/tenants")
    assert resp.status_code == 200

    tenants = resp.json()
    # Walker B também tem active em TENANT_A, mas o endpoint deve filtrar por user.id
    # Se houvesse vazamento, veríamos 2 entradas para TENANT_A
    tenant_a_entries = [t for t in tenants if t["tenant_id"] == TENANT_A_ID]
    assert len(tenant_a_entries) == 1, (
        f"Esperado 1 entry para tenant A (só walker A), obteve {len(tenant_a_entries)}"
    )


def test_walker_tenants_nao_retorna_declined():
    """GET /walker/tenants: status=declined não aparece (apenas active)."""
    db = _build_db()
    _populate(db)

    # Cria outro tenant com acesso declined
    TENANT_C_ID = "tenant-step3-c"
    db.add(Tenant(id=TENANT_C_ID, name="Rede C", slug="rede-c", status="active", plan="starter"))
    db.add(TenantWalkerAccess(
        id="twa-step3-declined",
        tenant_id=TENANT_C_ID,
        walker_user_id=WALKER_ID,
        status="declined",
        access_type="shared_network",
    ))
    db.commit()

    client = _make_app_and_client(db)
    resp = client.get("/walker/tenants")
    assert resp.status_code == 200

    tenant_ids = [t["tenant_id"] for t in resp.json()]
    assert TENANT_C_ID not in tenant_ids, "acesso declined NÃO deve aparecer"


# ═══════════════════════════════════════════════════════════════════════════════
# 5. Guard: /walker/tenants exige role walker
# ═══════════════════════════════════════════════════════════════════════════════


def test_walker_tenants_bloqueia_nao_walker():
    """GET /walker/tenants: usuário sem perfil de walker é barrado (403)."""
    db = _build_db()
    _populate(db)

    # Tutor não tem WalkerProfile — deve ser bloqueado pelo _require_active_walker
    client = _make_app_non_walker(db)
    resp = client.get("/walker/tenants")
    assert resp.status_code == 403
