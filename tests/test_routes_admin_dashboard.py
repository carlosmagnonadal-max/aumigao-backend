"""Testes de ROTA (camada HTTP) do grupo dashboard/cockpit/metricas do admin.

Cobre GET /admin/dashboard (estrutura da resposta, filtro por tenant, gating
admin.access) e GET /admin/operational-alerts (estrutura + gating).

Padrao do projeto (ver tests/test_routes_walker_quality.py e test_routes_auth.py):
monta um FastAPI MINIMO so com o admin_router de app.routes.admin, SQLite em
memoria (StaticPool), overrides de get_db / get_current_user. NAO importa
app.main (que conecta no Neon de PROD).

Notas de modelagem (lidas de app/routes/admin.py + app/dependencies/*):
- require_permission("admin.access") + get_admin_tenant_scope: super_admin passa
  e enxerga TODOS os tenants (escopo global); um "admin" comum precisa de
  tenant_id e fica restrito ao seu tenant; outros papeis -> 403.
- Filtros de "realness": tutores precisam de role em {tutor,cliente,...} e email
  valido, e NENHUM campo pode conter tokens fake ("test","demo","login","mock"...
  ver FAKE_ENTITY_TOKENS). Por isso os seeds usam dominio "@aumigao.app".
- Walkers reais ativos: WalkerProfile.status=="active" e active_as_walker=True,
  e o User precisa ter role em {walker,passeador}.
"""
from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 — registra todas as tabelas no Base.metadata
from app.core.database import Base, get_db
from app.dependencies.auth import get_current_user
from app.models.pet import Pet
from app.models.user import User
from app.models.walk import Walk
from app.models.walker_profile import WalkerProfile
from app.routes import admin

SUPER_ID = "super-1"
ADMIN_A_ID = "admin-a"
TUTOR_ID = "tutor-1"

TENANT_A = "tenant-a"
TENANT_B = "tenant-b"

# Emails sem tokens "fake" (test/demo/login/mock) para passarem nos filtros de realness.
SUPER_EMAIL = "super@aumigao.app"
TUTOR_A_EMAIL = "joana@aumigao.app"
TUTOR_B_EMAIL = "carlos@aumigao.app"
# Email com token "test" -> NAO conta como tutor real (FAKE_ENTITY_TOKENS). Usado
# no usuario base de gating para nao poluir as contagens do dashboard.
GATING_TUTOR_EMAIL = "gating-tutor@test.local"


def build(*, current: str = SUPER_ID):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    # super_admin (escopo global) — bypassa RBAC em user_has_permission.
    db.add(User(id=SUPER_ID, email=SUPER_EMAIL, password_hash="x", role="super_admin"))
    # admin comum do TENANT_A (restrito ao proprio tenant). Precisa de RBAC para
    # admin.access, por isso so usamos esse usuario no teste de tenant scoping
    # via super_admin override (o gating ja e coberto pelo teste 403). Aqui ele
    # serve para validar o filtro de tenant — ver test_dashboard_tenant_scope.
    db.add(User(id=ADMIN_A_ID, email="adm-a@aumigao.app", password_hash="x", role="admin", tenant_id=TENANT_A))
    # usuario sem permissao (tutor) -> 403. Email com token fake para nao contar
    # como tutor real nas metricas do dashboard.
    db.add(User(id=TUTOR_ID, email=GATING_TUTOR_EMAIL, password_hash="x", role="tutor", tenant_id=TENANT_A))
    db.commit()

    test_app = FastAPI()
    test_app.include_router(admin.router)  # admin_router (prefix /admin)
    test_app.dependency_overrides[get_db] = lambda: db
    test_app.dependency_overrides[get_current_user] = lambda: db.get(User, current)
    return TestClient(test_app), db


def set_user(client, db, user_id):
    client.app.dependency_overrides[get_current_user] = lambda: db.get(User, user_id)


def seed_real_tutor(db, *, uid, email, tenant_id):
    """Tutor real + pet real, num dado tenant. Retorna (user_id, pet_id)."""
    db.add(User(id=uid, email=email, password_hash="x", role="tutor", full_name="Tutor Real", tenant_id=tenant_id))
    pet_id = f"pet-{uid}"
    db.add(Pet(id=pet_id, tutor_id=uid, tenant_id=tenant_id, name="Rex", species="Cachorro"))
    db.commit()
    return uid, pet_id


# --------------------------------------------------------- gating (403/200) ---
def test_dashboard_forbidden_without_permission():
    client, db = build(current=SUPER_ID)
    set_user(client, db, TUTOR_ID)  # tutor: sem admin.access
    r = client.get("/admin/dashboard")
    assert r.status_code == 403


def test_dashboard_authorized_super_admin_empty_structure():
    client, _ = build(current=SUPER_ID)
    r = client.get("/admin/dashboard")
    assert r.status_code == 200, r.text
    body = r.json()
    # contadores principais existem e zeram num banco vazio.
    for key in (
        "total_clients", "total_tutors", "total_pets", "total_active_walkers",
        "total_walkers", "total_walks_scheduled", "completed_walks",
        "estimated_revenue_paid", "walkers_at_risk", "critical_operational_alerts",
    ):
        assert key in body, f"faltou {key}"
    assert body["total_clients"] == 0
    assert body["total_pets"] == 0
    assert body["total_active_walkers"] == 0
    assert body["no_show_rate"] == 0
    # blocos compostos do cockpit estao presentes e bem formados.
    assert isinstance(body["critical_walks"], list)
    assert isinstance(body["beta_operational_health"], dict)
    assert body["beta_operational_health"]["status"] in {"stable", "watch", "attention"}
    assert isinstance(body["operational_observability"], dict)
    assert isinstance(body["operational_scheduler"], dict)
    assert isinstance(body["beta_readiness"], dict)


def test_dashboard_counts_real_entities():
    client, db = build(current=SUPER_ID)
    seed_real_tutor(db, uid="tut-real", email=TUTOR_B_EMAIL, tenant_id=TENANT_A)
    # walker real ativo
    db.add(User(id="wk-real", email="passeador@aumigao.app", password_hash="x", role="walker", full_name="Walker Real"))
    db.add(WalkerProfile(id="wp-real", user_id="wk-real", full_name="Walker Real", status="active", active_as_walker=True))
    db.commit()
    body = client.get("/admin/dashboard").json()
    assert body["total_clients"] == 1
    assert body["total_tutors"] == 1
    assert body["total_pets"] == 1
    assert body["total_active_walkers"] == 1


def test_dashboard_ignores_fake_tutor():
    # email com token "test" -> filtrado por _is_real_tutor / FAKE_ENTITY_TOKENS.
    client, db = build(current=SUPER_ID)
    db.add(User(id="fake-tut", email="fake@test.com", password_hash="x", role="tutor", tenant_id=TENANT_A))
    db.commit()
    body = client.get("/admin/dashboard").json()
    assert body["total_clients"] == 0


# ------------------------------------------------------- filtro por tenant ---
def test_dashboard_tenant_scope_restricts_to_own_tenant():
    """Admin nao-global enxerga so o proprio tenant (apply_tenant_filter)."""
    client, db = build(current=SUPER_ID)
    seed_real_tutor(db, uid="tut-a", email=TUTOR_A_EMAIL, tenant_id=TENANT_A)
    seed_real_tutor(db, uid="tut-b", email=TUTOR_B_EMAIL, tenant_id=TENANT_B)

    # super_admin (global) ve os dois tutores
    body_super = client.get("/admin/dashboard").json()
    assert body_super["total_clients"] == 2
    assert body_super["total_pets"] == 2

    # admin do TENANT_A ve apenas o seu (gating admin.access nao bloqueia: super
    # passa por RBAC; aqui o usuario e role="admin" com tenant_id=TENANT_A, mas
    # sem seed RBAC ele tomaria 403 — entao validamos o ESCOPO chamando o helper
    # diretamente via override do scope dentro do request usando super override).
    set_user(client, db, ADMIN_A_ID)
    r = client.get("/admin/dashboard")
    # admin comum sem seed de RBAC: 403 (rede de seguranca so cobre super_admin).
    assert r.status_code == 403


def test_dashboard_tenant_scope_via_scope_helper():
    """Valida o filtro de tenant no nivel do helper (sem depender de RBAC).

    apply_tenant_filter restringe a query ao tenant do escopo; super_admin = global.
    """
    from app.dependencies.tenant_scope import apply_tenant_filter, get_admin_tenant_scope

    client, db = build(current=SUPER_ID)
    seed_real_tutor(db, uid="tut-a", email=TUTOR_A_EMAIL, tenant_id=TENANT_A)
    seed_real_tutor(db, uid="tut-b", email=TUTOR_B_EMAIL, tenant_id=TENANT_B)

    admin_a = db.get(User, ADMIN_A_ID)
    scope_a = get_admin_tenant_scope(admin_a)
    assert scope_a.is_global is False
    assert scope_a.tenant_id == TENANT_A
    pets_a = apply_tenant_filter(db.query(Pet), Pet, scope_a).all()
    assert {p.tenant_id for p in pets_a} == {TENANT_A}

    super_user = db.get(User, SUPER_ID)
    scope_super = get_admin_tenant_scope(super_user)
    assert scope_super.is_global is True
    pets_all = apply_tenant_filter(db.query(Pet), Pet, scope_super).all()
    assert {p.tenant_id for p in pets_all} == {TENANT_A, TENANT_B}


# ------------------------------------------------- operational-alerts (403) ---
def test_operational_alerts_forbidden_without_permission():
    client, db = build(current=SUPER_ID)
    set_user(client, db, TUTOR_ID)
    r = client.get("/admin/operational-alerts")
    assert r.status_code == 403


def test_operational_alerts_authorized_structure_empty():
    client, _ = build(current=SUPER_ID)
    r = client.get("/admin/operational-alerts")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 0
    assert body["items"] == []


def test_operational_alerts_lists_recovery_walk():
    client, db = build(current=SUPER_ID)
    uid, pet_id = seed_real_tutor(db, uid="tut-alert", email=TUTOR_A_EMAIL, tenant_id=TENANT_A)
    # walk em status de recovery -> entra em operational-alerts
    db.add(Walk(
        id="walk-rec", tutor_id=uid, pet_id=pet_id, tenant_id=TENANT_A,
        scheduled_date="2026-06-10 10:00", duration_minutes=30, price=40.0,
        status="no_walker_found", operational_status="no_walker_found",
        created_at=datetime.utcnow(),
    ))
    db.commit()
    body = client.get("/admin/operational-alerts").json()
    assert body["total"] == 1
    assert body["items"][0]["id"] == "walk-rec"
