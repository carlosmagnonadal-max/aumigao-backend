"""Testes TDD para seed_base_recurring_plans e BASE_RECURRING_PLANS.

Monta SQLite in-memory com as tabelas relevantes (Tenant, TenantFeature,
RecurringPlan) — mesmo padrão de tests/test_recurring_plans.py.

Cobertura:
- test_seed_creates_nine_base_plans       — tenant elegível sem planos → 9 criados
- test_seed_is_idempotent                 — 2ª chamada retorna 0 / total fica 9
- test_seed_skips_when_tenant_has_plans   — já tem plano → retorna 0 sem adicionar
- test_seed_skips_when_plan_ineligible    — plano inelegível → retorna 0

Hook (create_tenant):
- test_create_tenant_seeds_plans_for_eligible_tenant — verifica que a rota de criação
  de tenant (via FastAPI TestClient) resulta em 9 recurring_plans para tenant elegível.
"""
import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.recurring_plan import RECURRING_PLANS_FEATURE_KEY, RecurringPlan
from app.models.tenant import Tenant, TenantFeature
from app.services.recurring_plan_seed import BASE_RECURRING_PLANS, seed_base_recurring_plans


# ── helpers ────────────────────────────────────────────────────────────────────

def _db():
    """SQLite in-memory com as 3 tabelas necessárias."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[
            Tenant.__table__,
            TenantFeature.__table__,
            RecurringPlan.__table__,
        ],
    )
    return sessionmaker(bind=engine)()


def _make_tenant(db, *, plan: str = "business", tenant_id: str = "t-seed") -> Tenant:
    """Cria e persiste um tenant com o plano indicado."""
    tenant = Tenant(id=tenant_id, name="Seed Co", slug=f"seed-co-{tenant_id}", status="active", plan=plan)
    db.add(tenant)
    db.commit()
    return tenant


# ── constante BASE_RECURRING_PLANS ─────────────────────────────────────────────

def test_base_recurring_plans_has_nine_entries():
    assert len(BASE_RECURRING_PLANS) == 9


def test_base_recurring_plans_expected_names():
    names = {p["name"] for p in BASE_RECURRING_PLANS}
    assert names == {
        "Leve Mensal", "Ativo Mensal", "Intenso Mensal",
        "Leve Semestral", "Ativo Semestral", "Intenso Semestral",
        "Leve Anual", "Ativo Anual", "Intenso Anual",
    }


def test_base_recurring_plans_intervals():
    intervals = {p["name"]: p["interval"] for p in BASE_RECURRING_PLANS}
    assert intervals["Leve Mensal"] == "monthly"
    assert intervals["Ativo Semestral"] == "semiannual"
    assert intervals["Intenso Anual"] == "yearly"


# ── seed_base_recurring_plans ──────────────────────────────────────────────────

def test_seed_creates_nine_base_plans():
    """Tenant elegível (plan=business) sem planos → seed cria exatamente 9 planos
    com nomes, preços, walks_per_cycle e intervals batendo com BASE_RECURRING_PLANS."""
    db = _db()
    tenant = _make_tenant(db, plan="business")

    created = seed_base_recurring_plans(db, tenant)

    assert created == 9, f"esperado 9, obtido {created}"
    db.commit()

    plans = db.query(RecurringPlan).filter(RecurringPlan.tenant_id == tenant.id).all()
    assert len(plans) == 9

    # Valida que cada entrada da constante foi criada fielmente
    db_map = {p.name: p for p in plans}
    for spec in BASE_RECURRING_PLANS:
        name = spec["name"]
        assert name in db_map, f"plano '{name}' não foi criado"
        rp = db_map[name]
        assert rp.price == spec["price"], f"{name}: preço diverge"
        assert rp.walks_per_cycle == spec["walks_per_cycle"], f"{name}: walks_per_cycle diverge"
        assert rp.interval == spec["interval"], f"{name}: interval diverge"
        assert rp.active is True, f"{name}: deveria ser active=True"
        assert rp.tenant_id == tenant.id


def test_seed_is_idempotent():
    """Rodar seed 2x no mesmo tenant deve manter exatamente 9 planos;
    a segunda chamada deve retornar 0."""
    db = _db()
    tenant = _make_tenant(db, plan="business")

    first = seed_base_recurring_plans(db, tenant)
    db.commit()

    second = seed_base_recurring_plans(db, tenant)
    db.commit()

    assert first == 9
    assert second == 0, "segunda chamada deve retornar 0 (idempotente)"

    count = db.query(RecurringPlan).filter(RecurringPlan.tenant_id == tenant.id).count()
    assert count == 9, f"idempotência violada: {count} planos após 2 chamadas"


def test_seed_skips_when_tenant_has_plans():
    """Se o tenant já tem qualquer plano, seed retorna 0 e NÃO adiciona novos."""
    db = _db()
    tenant = _make_tenant(db, plan="business")
    # Insere 1 plano pré-existente
    existing = RecurringPlan(
        tenant_id=tenant.id,
        name="Plano Existente",
        price=99.0,
        walks_per_cycle=5,
        interval="monthly",
    )
    db.add(existing)
    db.commit()

    result = seed_base_recurring_plans(db, tenant)
    db.commit()

    assert result == 0, "deve retornar 0 quando tenant já tem planos"
    count = db.query(RecurringPlan).filter(RecurringPlan.tenant_id == tenant.id).count()
    assert count == 1, "não deve adicionar planos quando já existem"


def test_seed_skips_when_plan_ineligible():
    """Tenant com plano inelegível (starter no modo v1) → seed retorna 0.

    No modo v1 (PRICING_V2_ENABLED=false, default), recurring_plans é gated em
    {business, enterprise}. Starter → plan_allows_product_feature retorna False.
    Garante que o env está em modo v1 para este teste.
    """
    env_backup = os.environ.get("PRICING_V2_ENABLED")
    try:
        # Força modo v1 explicitamente
        os.environ["PRICING_V2_ENABLED"] = "false"
        # Reimporta o serviço DEPOIS de setar o env, pois a flag é lida no import
        import importlib
        import app.services.tenant_plan_service as tps
        importlib.reload(tps)
        import app.services.recurring_plan_seed as rps
        importlib.reload(rps)

        db = _db()
        tenant = _make_tenant(db, plan="starter")

        result = rps.seed_base_recurring_plans(db, tenant)
        db.commit()

        assert result == 0, "starter deve retornar 0 (plano inelegível)"
        count = db.query(RecurringPlan).filter(RecurringPlan.tenant_id == tenant.id).count()
        assert count == 0, "nenhum plano deve ter sido criado para starter"
    finally:
        if env_backup is None:
            os.environ.pop("PRICING_V2_ENABLED", None)
        else:
            os.environ["PRICING_V2_ENABLED"] = env_backup
        # Restaura os módulos ao estado original
        import importlib
        import app.services.tenant_plan_service as tps
        importlib.reload(tps)
        import app.services.recurring_plan_seed as rps
        importlib.reload(rps)


# ── hook create_tenant ─────────────────────────────────────────────────────────

def test_create_tenant_seeds_plans_for_eligible_tenant():
    """Cria um tenant via rota POST /admin/tenants com plan=business e verifica
    que nasceu com 9 recurring_plans seeded.

    Usa FastAPI TestClient + SQLite in-memory (mesmo padrão de
    tests/test_routes_admin_tenants.py). O escopo RLS global do super_admin
    é injetado pela rota (get_admin_tenant_scope) antes do INSERT, garantindo
    que o seed transacional passa no WITH CHECK de recurring_plans.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy.pool import StaticPool

    import app.models  # noqa: F401 — registra todas as tabelas no Base.metadata
    from app.core.database import get_db
    from app.dependencies.auth import get_current_user
    from app.models.user import User
    from app.routes import tenants as tenants_router

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    ADMIN_ID = "admin-seed-test"
    db.add(User(id=ADMIN_ID, email="admin@seed.com", password_hash="x", role="super_admin"))
    db.commit()

    test_app = FastAPI()
    test_app.include_router(tenants_router.router)
    test_app.dependency_overrides[get_db] = lambda: db
    test_app.dependency_overrides[get_current_user] = lambda: db.get(User, ADMIN_ID)
    client = TestClient(test_app)

    r = client.post(
        "/admin/tenants",
        json={"name": "Seed Tenant", "slug": "seed-tenant-hook", "status": "active", "plan": "business"},
    )
    assert r.status_code == 200, f"criação falhou: {r.text}"

    tenant_id = r.json()["id"]
    db.expire_all()
    count = db.query(RecurringPlan).filter(RecurringPlan.tenant_id == tenant_id).count()
    assert count == 9, (
        f"esperado 9 recurring_plans após create_tenant, obtido {count}. "
        "Verifique se seed_base_recurring_plans está sendo chamado na rota."
    )
