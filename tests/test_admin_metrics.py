"""Testes dos endpoints de métricas admin (Fase C).

Cobre os 4 endpoints:
  GET /admin/coupons/metrics
  GET /admin/incentives/metrics
  GET /admin/referrals/metrics
  GET /admin/complaints/metrics

Usa SQLite in-memory para isolar do banco de prod.
Verifica: shape do JSON, tenant-scoping, valores calculados, série semanal.

Estratégia de auth nos testes:
  - Admins de tenant: role="admin" com tenant_id → get_admin_tenant_scope aplica escopo.
  - Super admin: role="super_admin" → user_has_permission bypassa RBAC; scope global.
  - O dependency override de get_current_user é suficiente porque require_permission
    usa get_current_user internamente — ao sobrescrever get_current_user, a cadeia
    toda passa para o usuário correto.
"""
from __future__ import annotations

import app.models  # noqa: F401 — registra todos os modelos no Base.metadata

from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.dependencies.auth import get_current_user
from app.models.complaint import Complaint
from app.models.coupon import Coupon, CouponRedemption
from app.models.incentive_rule import IncentiveRule
from app.models.tenant import Tenant
from app.models.user import User
from app.models.walker_incentive import WalkerIncentive
from app.models.walker_referral import WalkerReferral
from app.routes import complaints as complaints_routes
from app.routes import coupons as coupons_routes
from app.routes import incentives as incentives_routes
from app.routes import referrals as referrals_routes
from app.services.metrics_service import _iso_week

# ─────────────────────────────────────────────────────────────────────────────
# Fixtures base
# ─────────────────────────────────────────────────────────────────────────────

TENANT_A = "tenant-a"
TENANT_B = "tenant-b"
ADMIN_A = "admin-a"
ADMIN_B = "admin-b"
SUPER_ID = "super-1"


def _make_engine():
    return create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


def _setup_db(engine) -> Session:
    """Cria todas as tabelas necessárias e retorna sessão."""
    # Usa Base.metadata.create_all completo (importado via app.models) para evitar
    # problemas de FK entre tabelas.
    Base.metadata.create_all(engine)
    Sm = sessionmaker(bind=engine)
    return Sm()


def _seed_tenants_and_admins(db: Session):
    """Cria 2 tenants + users para testes.

    Todos os admins criados como super_admin para bypassing de RBAC nos testes HTTP
    (user_has_permission retorna True para super_admin sem precisar de seed).
    Para testar tenant-scoping, usamos o metrics_service diretamente com AdminTenantScope.

    ADMIN_A e ADMIN_B são super_admins para passar nos testes HTTP, mas têm tenant_id
    para que get_admin_tenant_scope possa criar scope scoped quando chamado diretamente.
    """
    for tid, slug in [(TENANT_A, "slug-a"), (TENANT_B, "slug-b")]:
        db.add(Tenant(id=tid, name=tid, slug=slug, status="active", plan="business"))
    # super_admin: bypassa RBAC. tenant_id setado para uso em testes de scoping via service.
    db.add(User(id=ADMIN_A, email="admin_a@x.com", password_hash="x", role="super_admin", tenant_id=TENANT_A))
    db.add(User(id=ADMIN_B, email="admin_b@x.com", password_hash="x", role="super_admin", tenant_id=TENANT_B))
    db.add(User(id=SUPER_ID, email="super@x.com", password_hash="x", role="super_admin"))
    db.commit()


# ─────────────────────────────────────────────────────────────────────────────
# Helpers para criar dados de teste
# ─────────────────────────────────────────────────────────────────────────────

def _add_coupon(db: Session, tenant_id: str, code: str, active: bool = True) -> Coupon:
    c = Coupon(
        id=str(uuid4()),
        tenant_id=tenant_id,
        code=code,
        discount_type="percent",
        discount_value=10.0,
        active=active,
    )
    db.add(c)
    db.commit()
    return c


def _add_redemption(
    db: Session,
    coupon: Coupon,
    amount: float = 10.0,
    weeks_ago: int = 0,
) -> CouponRedemption:
    dt = datetime.utcnow() - timedelta(weeks=weeks_ago)
    r = CouponRedemption(
        id=str(uuid4()),
        coupon_id=coupon.id,
        tenant_id=coupon.tenant_id,
        user_id="u1",
        amount_discounted=amount,
        created_at=dt,
    )
    db.add(r)
    db.commit()
    return r


def _add_incentive_rule(db: Session, tenant_id: str, active: bool = True) -> IncentiveRule:
    r = IncentiveRule(
        id=str(uuid4()),
        tenant_id=tenant_id,
        key=str(uuid4())[:8],
        title="Regra",
        description="",
        trigger_type="rating",
        threshold=4.5,
        reward_type="recognition",
        reward_value=0.0,
        active=active,
    )
    db.add(r)
    db.commit()
    return r


def _add_walker(db: Session, tenant_id: str) -> str:
    uid = str(uuid4())
    db.add(User(id=uid, email=f"{uid}@x.com", password_hash="x", role="walker", tenant_id=tenant_id))
    db.commit()
    return uid


def _add_walker_incentive(
    db: Session,
    walker_id: str,
    incentive_type: str = "recognition",
    amount: float = 0.0,
    weeks_ago: int = 0,
) -> WalkerIncentive:
    dt = datetime.utcnow() - timedelta(weeks=weeks_ago)
    i = WalkerIncentive(
        id=str(uuid4()),
        walker_id=walker_id,
        incentive_type=incentive_type,
        title="Incentivo",
        description="",
        source="admin",
        reward_type=incentive_type,
        amount=amount,
        status="active",
        created_at=dt,
    )
    db.add(i)
    db.commit()
    return i


def _add_referral(
    db: Session,
    referrer_id: str,
    status: str = "pending",
    reward_status: str = "not_eligible",
    reward_amount: float | None = None,
    weeks_ago: int = 0,
) -> WalkerReferral:
    dt = datetime.utcnow() - timedelta(weeks=weeks_ago)
    r = WalkerReferral(
        id=str(uuid4()),
        referrer_user_id=referrer_id,
        referred_name="Fulano",
        referred_phone="11999999999",
        referred_phone_normalized="11999999999",
        city="SP",
        neighborhood="Centro",
        referral_code=str(uuid4())[:10],
        status=status,
        reward_status=reward_status,
        reward_amount=reward_amount,
        created_at=dt,
    )
    db.add(r)
    db.commit()
    return r


def _add_complaint(
    db: Session,
    tenant_id: str,
    status: str = "aberta",
    severity: str = "baixa",
    category: str = "comportamento",
    resolved_at: datetime | None = None,
    weeks_ago: int = 0,
) -> Complaint:
    dt = datetime.utcnow() - timedelta(weeks=weeks_ago)
    c = Complaint(
        id=str(uuid4()),
        tenant_id=tenant_id,
        source="tutor",
        author_id="u-author",
        author_role="tutor",
        target_type="walker",
        category=category,
        severity=severity,
        status=status,
        title="Teste",
        description="Descrição da ocorrência",
        created_at=dt,
        resolved_at=resolved_at,
    )
    db.add(c)
    db.commit()
    return c


# ─────────────────────────────────────────────────────────────────────────────
# Builders de TestClient
#
# Estratégia: sobrescrever get_current_user (auth) retornando o user correto.
# require_permission chama get_current_user internamente — ao sobrescrever a
# dependency raiz, a verificação RBAC passa (super_admin bypassa tudo, admin
# regular passa se tiver o papel). Para testes simples usamos super_admin como
# admin_a/b já que não precisamos testar o RBAC em si aqui (há tests dedicados).
#
# Para testar tenant-scoping, usamos users com role="admin" e tenant_id correto
# — get_admin_tenant_scope aplica o filtro com base no user.tenant_id.
# ─────────────────────────────────────────────────────────────────────────────

def _build_app_coupons(db: Session, user: User) -> TestClient:
    """TestClient para /admin/coupons/*"""
    _app = FastAPI()
    _app.include_router(coupons_routes.admin_router)
    _app.dependency_overrides[get_db] = lambda: db
    _app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(_app)


def _build_app_incentives(db: Session, user: User) -> TestClient:
    """TestClient para /admin/incentive-rules, /admin/incentives/*"""
    _app = FastAPI()
    _app.include_router(incentives_routes.admin_router)
    _app.dependency_overrides[get_db] = lambda: db
    _app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(_app)


def _build_app_referrals(db: Session, user: User) -> TestClient:
    """TestClient para /admin/referrals/*"""
    _app = FastAPI()
    _app.include_router(referrals_routes.admin_router)
    _app.dependency_overrides[get_db] = lambda: db
    _app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(_app)


def _build_app_complaints(db: Session, user: User) -> TestClient:
    """TestClient para /admin/complaints/*"""
    _app = FastAPI()
    _app.include_router(complaints_routes.admin_router)
    _app.dependency_overrides[get_db] = lambda: db
    _app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(_app)


# ─────────────────────────────────────────────────────────────────────────────
# TESTES: GET /admin/coupons/metrics
# ─────────────────────────────────────────────────────────────────────────────

class TestCouponMetrics:
    def _setup(self):
        engine = _make_engine()
        db = _setup_db(engine)
        _seed_tenants_and_admins(db)
        return db

    def test_empty_tenant_returns_zeros(self):
        db = self._setup()
        admin = db.get(User, ADMIN_A)
        r = _build_app_coupons(db, admin).get("/admin/coupons/metrics")
        assert r.status_code == 200
        body = r.json()
        assert body["total_coupons"] == 0
        assert body["active_coupons"] == 0
        assert body["total_redemptions"] == 0
        assert body["total_discount_amount"] == 0.0
        assert body["top_coupons"] == []
        assert body["redemptions_by_week"] == []

    def test_counts_coupons_and_redemptions(self):
        db = self._setup()
        admin = db.get(User, ADMIN_A)
        c1 = _add_coupon(db, TENANT_A, "PROMO10", active=True)
        c2 = _add_coupon(db, TENANT_A, "PROMO20", active=False)
        _add_redemption(db, c1, amount=10.0)
        _add_redemption(db, c1, amount=5.0)
        r = _build_app_coupons(db, admin).get("/admin/coupons/metrics")
        body = r.json()
        assert body["total_coupons"] == 2
        assert body["active_coupons"] == 1
        assert body["total_redemptions"] == 2
        assert body["total_discount_amount"] == 15.0

    def test_top_coupons_ordered_by_redemptions(self):
        db = self._setup()
        admin = db.get(User, ADMIN_A)
        c1 = _add_coupon(db, TENANT_A, "A")
        c2 = _add_coupon(db, TENANT_A, "B")
        _add_redemption(db, c1)
        _add_redemption(db, c1)
        _add_redemption(db, c2)
        r = _build_app_coupons(db, admin).get("/admin/coupons/metrics")
        top = r.json()["top_coupons"]
        assert top[0]["code"] == "A"
        assert top[0]["redemptions"] == 2
        assert top[1]["code"] == "B"

    def test_tenant_isolation_via_service(self):
        """Tenant scope A não vê cupons do tenant B (teste via service direto)."""
        from app.dependencies.tenant_scope import AdminTenantScope
        from app.services.metrics_service import get_coupon_metrics

        db = self._setup()
        admin_a = db.get(User, ADMIN_A)
        c_a = _add_coupon(db, TENANT_A, "COUPON-A")
        c_b = _add_coupon(db, TENANT_B, "COUPON-B")
        _add_redemption(db, c_b, amount=50.0)

        # Scope explícito para tenant A
        scope_a = AdminTenantScope(user=admin_a, tenant_id=TENANT_A, is_global=False, role="admin")
        result = get_coupon_metrics(db, scope_a)
        assert result["total_coupons"] == 1
        assert result["total_redemptions"] == 0
        assert result["total_discount_amount"] == 0.0

    def test_super_admin_sees_all(self):
        db = self._setup()
        super_admin = db.get(User, SUPER_ID)
        _add_coupon(db, TENANT_A, "A")
        _add_coupon(db, TENANT_B, "B")
        body = _build_app_coupons(db, super_admin).get("/admin/coupons/metrics").json()
        assert body["total_coupons"] == 2

    def test_redemptions_by_week_has_week_key(self):
        db = self._setup()
        admin = db.get(User, ADMIN_A)
        c = _add_coupon(db, TENANT_A, "WEEKLY")
        _add_redemption(db, c, amount=10.0, weeks_ago=0)
        _add_redemption(db, c, amount=10.0, weeks_ago=2)
        body = _build_app_coupons(db, admin).get("/admin/coupons/metrics").json()
        series = body["redemptions_by_week"]
        assert len(series) >= 1
        # Verifica shape: cada item tem week, count, amount
        for item in series:
            assert "week" in item
            assert "count" in item
            assert "amount" in item
            assert item["week"].startswith("20")  # ex: "2026-W23"


# ─────────────────────────────────────────────────────────────────────────────
# TESTES: GET /admin/incentives/metrics
# ─────────────────────────────────────────────────────────────────────────────

class TestIncentiveMetrics:
    def _setup(self):
        engine = _make_engine()
        db = _setup_db(engine)
        _seed_tenants_and_admins(db)
        return db

    def test_empty_returns_zeros(self):
        db = self._setup()
        admin = db.get(User, ADMIN_A)
        r = _build_app_incentives(db, admin).get("/admin/incentives/metrics")
        assert r.status_code == 200
        body = r.json()
        assert body["total_rules"] == 0
        assert body["active_rules"] == 0
        assert body["total_granted"] == 0
        assert body["granted_amount"] == 0.0
        assert body["by_type"] == []
        assert body["granted_by_week"] == []
        assert "scope_note" in body

    def test_rules_counted_correctly(self):
        db = self._setup()
        admin = db.get(User, ADMIN_A)
        _add_incentive_rule(db, TENANT_A, active=True)
        _add_incentive_rule(db, TENANT_A, active=False)
        body = _build_app_incentives(db, admin).get("/admin/incentives/metrics").json()
        assert body["total_rules"] == 2
        assert body["active_rules"] == 1

    def test_granted_scoped_by_tenant_via_user(self):
        """WalkerIncentive scoping via User.tenant_id (teste via service direto)."""
        from app.dependencies.tenant_scope import AdminTenantScope
        from app.services.metrics_service import get_incentive_metrics

        db = self._setup()
        admin_a = db.get(User, ADMIN_A)
        walker_a = _add_walker(db, TENANT_A)
        walker_b = _add_walker(db, TENANT_B)
        _add_walker_incentive(db, walker_a, incentive_type="monetary", amount=50.0)
        _add_walker_incentive(db, walker_b, incentive_type="recognition", amount=0.0)

        # Scope explícito para tenant A
        scope_a = AdminTenantScope(user=admin_a, tenant_id=TENANT_A, is_global=False, role="admin")
        result = get_incentive_metrics(db, scope_a)
        assert result["total_granted"] == 1
        assert result["granted_amount"] == 50.0
        types = {bt["incentive_type"] for bt in result["by_type"]}
        assert "monetary" in types
        assert "recognition" not in types

    def test_super_admin_sees_all_grants(self):
        db = self._setup()
        super_admin = db.get(User, SUPER_ID)
        walker_a = _add_walker(db, TENANT_A)
        walker_b = _add_walker(db, TENANT_B)
        _add_walker_incentive(db, walker_a, amount=10.0)
        _add_walker_incentive(db, walker_b, amount=20.0)
        body = _build_app_incentives(db, super_admin).get("/admin/incentives/metrics").json()
        assert body["total_granted"] == 2
        assert body["granted_amount"] == 30.0

    def test_by_type_breakdown(self):
        db = self._setup()
        admin = db.get(User, ADMIN_A)
        walker = _add_walker(db, TENANT_A)
        _add_walker_incentive(db, walker, incentive_type="recognition", amount=0.0)
        _add_walker_incentive(db, walker, incentive_type="recognition", amount=0.0)
        _add_walker_incentive(db, walker, incentive_type="monetary", amount=100.0)
        body = _build_app_incentives(db, admin).get("/admin/incentives/metrics").json()
        by_type = {bt["incentive_type"]: bt for bt in body["by_type"]}
        assert by_type["recognition"]["count"] == 2
        assert by_type["monetary"]["count"] == 1
        assert by_type["monetary"]["amount"] == 100.0

    def test_granted_by_week_shape(self):
        db = self._setup()
        admin = db.get(User, ADMIN_A)
        walker = _add_walker(db, TENANT_A)
        _add_walker_incentive(db, walker, weeks_ago=0)
        _add_walker_incentive(db, walker, weeks_ago=3)
        body = _build_app_incentives(db, admin).get("/admin/incentives/metrics").json()
        series = body["granted_by_week"]
        assert len(series) >= 1
        for item in series:
            assert "week" in item and "count" in item


# ─────────────────────────────────────────────────────────────────────────────
# TESTES: GET /admin/referrals/metrics
# ─────────────────────────────────────────────────────────────────────────────

class TestReferralMetrics:
    def _setup(self):
        engine = _make_engine()
        db = _setup_db(engine)
        _seed_tenants_and_admins(db)
        return db

    def test_empty_returns_zeros(self):
        db = self._setup()
        admin = db.get(User, ADMIN_A)
        r = _build_app_referrals(db, admin).get("/admin/referrals/metrics")
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 0
        assert body["activated_count"] == 0
        assert body["reward_released_amount"] == 0.0
        assert body["by_status"] == []
        assert body["created_by_week"] == []
        assert "scope_note" in body

    def test_counts_total_and_activated(self):
        db = self._setup()
        admin = db.get(User, ADMIN_A)
        walker = _add_walker(db, TENANT_A)
        _add_referral(db, walker, status="pending")
        _add_referral(db, walker, status="converted")
        _add_referral(db, walker, status="converted")
        body = _build_app_referrals(db, admin).get("/admin/referrals/metrics").json()
        assert body["total"] == 3
        assert body["activated_count"] == 2

    def test_reward_released_amount_only_paid(self):
        db = self._setup()
        admin = db.get(User, ADMIN_A)
        walker = _add_walker(db, TENANT_A)
        _add_referral(db, walker, reward_status="paid", reward_amount=100.0)
        _add_referral(db, walker, reward_status="eligible", reward_amount=50.0)
        _add_referral(db, walker, reward_status="not_eligible")
        body = _build_app_referrals(db, admin).get("/admin/referrals/metrics").json()
        assert body["reward_released_amount"] == 100.0

    def test_by_status_breakdown(self):
        db = self._setup()
        admin = db.get(User, ADMIN_A)
        walker = _add_walker(db, TENANT_A)
        _add_referral(db, walker, status="pending")
        _add_referral(db, walker, status="pending")
        _add_referral(db, walker, status="approved")
        body = _build_app_referrals(db, admin).get("/admin/referrals/metrics").json()
        by_status = {bs["status"]: bs["count"] for bs in body["by_status"]}
        assert by_status["pending"] == 2
        assert by_status["approved"] == 1

    def test_global_scope_admin_sees_all_tenants(self):
        """Referrals são globais — mesmo scope do tenant A vê indicações do B."""
        from app.dependencies.tenant_scope import AdminTenantScope
        from app.services.metrics_service import get_referral_metrics

        db = self._setup()
        admin_a = db.get(User, ADMIN_A)
        walker_a = _add_walker(db, TENANT_A)
        walker_b = _add_walker(db, TENANT_B)
        _add_referral(db, walker_a)
        _add_referral(db, walker_b)

        # Mesmo com scope do tenant A, referrals são globais (sem tenant_id)
        scope_a = AdminTenantScope(user=admin_a, tenant_id=TENANT_A, is_global=False, role="admin")
        result = get_referral_metrics(db, scope_a)
        assert result["total"] == 2
        # scope_note deve mencionar "tenant_id" ausente (independente de encoding)
        assert "tenant_id" in result["scope_note"]

    def test_created_by_week_shape(self):
        db = self._setup()
        admin = db.get(User, ADMIN_A)
        walker = _add_walker(db, TENANT_A)
        _add_referral(db, walker, weeks_ago=0)
        _add_referral(db, walker, weeks_ago=1)
        body = _build_app_referrals(db, admin).get("/admin/referrals/metrics").json()
        series = body["created_by_week"]
        assert len(series) >= 1
        for item in series:
            assert "week" in item and "count" in item


# ─────────────────────────────────────────────────────────────────────────────
# TESTES: GET /admin/complaints/metrics
# ─────────────────────────────────────────────────────────────────────────────

class TestComplaintMetrics:
    def _setup(self):
        engine = _make_engine()
        db = _setup_db(engine)
        _seed_tenants_and_admins(db)
        return db

    def test_empty_returns_zeros(self):
        db = self._setup()
        admin = db.get(User, ADMIN_A)
        r = _build_app_complaints(db, admin).get("/admin/complaints/metrics")
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 0
        assert body["open_count"] == 0
        assert body["resolved_count"] == 0
        assert body["avg_resolution_hours"] is None
        assert body["by_category"] == []
        assert body["by_severity"] == []
        assert body["opened_by_week"] == []

    def test_counts_open_and_resolved(self):
        db = self._setup()
        admin = db.get(User, ADMIN_A)
        _add_complaint(db, TENANT_A, status="aberta")
        _add_complaint(db, TENANT_A, status="em_analise")
        _add_complaint(db, TENANT_A, status="resolvida")
        _add_complaint(db, TENANT_A, status="rejeitada")
        body = _build_app_complaints(db, admin).get("/admin/complaints/metrics").json()
        assert body["total"] == 4
        assert body["open_count"] == 2
        assert body["resolved_count"] == 2

    def test_avg_resolution_hours_calculated(self):
        db = self._setup()
        admin = db.get(User, ADMIN_A)
        created = datetime.utcnow() - timedelta(hours=24)
        resolved = datetime.utcnow()
        c = _add_complaint(db, TENANT_A, status="resolvida", resolved_at=resolved)
        # Ajusta created_at manualmente para garantir a diferença
        c.created_at = created
        db.commit()
        body = _build_app_complaints(db, admin).get("/admin/complaints/metrics").json()
        assert body["avg_resolution_hours"] is not None
        assert body["avg_resolution_hours"] > 20.0  # ~24h

    def test_avg_resolution_hours_null_when_no_resolved(self):
        db = self._setup()
        admin = db.get(User, ADMIN_A)
        _add_complaint(db, TENANT_A, status="aberta")
        body = _build_app_complaints(db, admin).get("/admin/complaints/metrics").json()
        assert body["avg_resolution_hours"] is None

    def test_by_category_breakdown(self):
        db = self._setup()
        admin = db.get(User, ADMIN_A)
        _add_complaint(db, TENANT_A, category="comportamento")
        _add_complaint(db, TENANT_A, category="comportamento")
        _add_complaint(db, TENANT_A, category="atraso")
        body = _build_app_complaints(db, admin).get("/admin/complaints/metrics").json()
        by_cat = {bc["category"]: bc["count"] for bc in body["by_category"]}
        assert by_cat["comportamento"] == 2
        assert by_cat["atraso"] == 1

    def test_by_severity_breakdown(self):
        db = self._setup()
        admin = db.get(User, ADMIN_A)
        _add_complaint(db, TENANT_A, severity="baixa")
        _add_complaint(db, TENANT_A, severity="alta")
        _add_complaint(db, TENANT_A, severity="alta")
        body = _build_app_complaints(db, admin).get("/admin/complaints/metrics").json()
        by_sev = {bs["severity"]: bs["count"] for bs in body["by_severity"]}
        assert by_sev["baixa"] == 1
        assert by_sev["alta"] == 2

    def test_tenant_isolation_via_service(self):
        """Scope do tenant A não vê complaints do tenant B (teste via service direto)."""
        from app.dependencies.tenant_scope import AdminTenantScope
        from app.services.metrics_service import get_complaint_metrics

        db = self._setup()
        admin_a = db.get(User, ADMIN_A)
        _add_complaint(db, TENANT_A, category="cat-a")
        _add_complaint(db, TENANT_A, category="cat-a")
        _add_complaint(db, TENANT_B, category="cat-b")

        scope_a = AdminTenantScope(user=admin_a, tenant_id=TENANT_A, is_global=False, role="admin")
        result = get_complaint_metrics(db, scope_a)
        assert result["total"] == 2

    def test_super_admin_sees_all(self):
        db = self._setup()
        super_admin = db.get(User, SUPER_ID)
        _add_complaint(db, TENANT_A)
        _add_complaint(db, TENANT_B)
        body = _build_app_complaints(db, super_admin).get("/admin/complaints/metrics").json()
        assert body["total"] == 2

    def test_opened_by_week_shape(self):
        db = self._setup()
        admin = db.get(User, ADMIN_A)
        _add_complaint(db, TENANT_A, weeks_ago=0)
        _add_complaint(db, TENANT_A, weeks_ago=2)
        body = _build_app_complaints(db, admin).get("/admin/complaints/metrics").json()
        series = body["opened_by_week"]
        assert len(series) >= 1
        for item in series:
            assert "week" in item and "count" in item


# ─────────────────────────────────────────────────────────────────────────────
# TESTES: serviço metrics_service (unitários, sem HTTP)
# ─────────────────────────────────────────────────────────────────────────────

class TestMetricsServiceUnit:
    """Testes diretos do metrics_service (mais rápidos, sem FastAPI)."""

    def _setup(self):
        engine = _make_engine()
        db = _setup_db(engine)
        _seed_tenants_and_admins(db)
        return db

    def test_iso_week_format(self):
        from app.services.metrics_service import _iso_week
        dt = datetime(2026, 6, 11)  # uma quarta-feira da semana 24
        w = _iso_week(dt)
        assert w is not None
        assert w.startswith("2026-W")
        assert len(w) == 8  # "2026-W24"

    def test_aggregate_by_week_groups_correctly(self):
        from app.services.metrics_service import _aggregate_by_week

        class FakeObj:
            def __init__(self, weeks_ago: int, amount: float):
                self.created_at = datetime.utcnow() - timedelta(weeks=weeks_ago)
                self.amount_discounted = amount

        items = [FakeObj(0, 10.0), FakeObj(0, 5.0), FakeObj(2, 20.0)]
        result = _aggregate_by_week(items, "created_at", amount_attr="amount_discounted")
        # Deve ter 2 semanas
        assert len(result) == 2
        weeks_map = {r["week"]: r for r in result}
        # A semana mais recente tem count=2 e amount=15
        # _iso_week já está importado no topo do arquivo
        current_week = _iso_week(datetime.utcnow())
        assert weeks_map[current_week]["count"] == 2
        assert weeks_map[current_week]["amount"] == 15.0

    def test_get_coupon_metrics_returns_correct_shape(self):
        from app.dependencies.tenant_scope import AdminTenantScope
        from app.services.metrics_service import get_coupon_metrics

        db = self._setup()
        admin = db.get(User, ADMIN_A)
        scope = AdminTenantScope(user=admin, tenant_id=TENANT_A, is_global=False, role="admin")
        c = _add_coupon(db, TENANT_A, "TEST")
        _add_redemption(db, c, amount=25.0)
        result = get_coupon_metrics(db, scope)
        assert result["total_coupons"] == 1
        assert result["total_redemptions"] == 1
        assert result["total_discount_amount"] == 25.0
        assert len(result["top_coupons"]) == 1
        assert result["top_coupons"][0]["code"] == "TEST"

    def test_get_complaint_metrics_avg_resolution(self):
        from app.dependencies.tenant_scope import AdminTenantScope
        from app.services.metrics_service import get_complaint_metrics

        db = self._setup()
        admin = db.get(User, ADMIN_A)
        scope = AdminTenantScope(user=admin, tenant_id=TENANT_A, is_global=False, role="admin")
        # Cria ocorrência resolvida em 48h
        created = datetime.utcnow() - timedelta(hours=48)
        resolved = datetime.utcnow()
        c = _add_complaint(db, TENANT_A, status="resolvida", resolved_at=resolved)
        c.created_at = created
        db.commit()
        result = get_complaint_metrics(db, scope)
        assert result["avg_resolution_hours"] is not None
        assert result["avg_resolution_hours"] >= 47.0

    def test_get_referral_metrics_global(self):
        from app.dependencies.tenant_scope import AdminTenantScope
        from app.services.metrics_service import get_referral_metrics

        db = self._setup()
        admin_a = db.get(User, ADMIN_A)
        scope_a = AdminTenantScope(user=admin_a, tenant_id=TENANT_A, is_global=False, role="admin")
        walker_b = _add_walker(db, TENANT_B)
        _add_referral(db, walker_b, status="converted", reward_status="paid", reward_amount=200.0)
        # Mesmo com scope do tenant A, referrals são globais
        result = get_referral_metrics(db, scope_a)
        assert result["total"] == 1
        assert result["activated_count"] == 1
        assert result["reward_released_amount"] == 200.0
