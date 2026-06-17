"""
Testes de isolamento multi-tenant — guardas de regressão (Onda 1 / mt-MT10).

Cobre:
  1. Admin do tenant A NAO consegue ler/alterar recurso do tenant B via /admin/tenants.
  2. Guard de saque (B-02): admin do tenant A nao aprova/rejeita saque do tenant B.
  3. apply_tenant_filter filtra por tenant_id quando scope NAO e global; e no-op quando is_global=True.
  4. Listagem de walks (tenant-scoped) so traz linhas do proprio tenant.

Padrao: FastAPI minimo + SQLite em memoria (StaticPool) + overrides de get_db /
get_current_user. Mesmo modelo usado em test_routes_admin_tenants.py e
test_tenant_isolation_walks.py — nenhuma infra nova criada.

Nao toca banco de producao (conftest.py aplica o guard anti-prod).
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, column as sa_column
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 — registra todas as tabelas no Base.metadata
from app.core.database import Base, get_db
from app.dependencies.auth import get_current_user
from app.dependencies.tenant_scope import (
    AdminTenantScope,
    apply_tenant_filter,
    ensure_tenant_access,
    get_admin_tenant_scope,
)
from app.models.payment import Payment
from app.models.rbac import Permission, Role, RolePermission, UserRoleAssignment
from app.models.tenant import Tenant
from app.models.user import User
from app.models.walk import Walk
from app.models.pet import Pet
from app.routes import tenants as tenants_router
from app.routes import admin as admin_router

# ────────────────────────────────────────────────────────────────────────────────
# IDs fixos para os cenários
# ────────────────────────────────────────────────────────────────────────────────
SUPER_ADMIN_ID = "sa-1"
ADMIN_A_ID = "admin-a-1"
ADMIN_B_ID = "admin-b-1"

TENANT_A_ID = "tenant-alpha"
TENANT_B_ID = "tenant-beta"

PAYMENT_B_ID = "pay-b-1"  # saque do tenant B


# ────────────────────────────────────────────────────────────────────────────────
# Fábrica de engine/sessão SQLite em memória (StaticPool — mesma conexão)
# ────────────────────────────────────────────────────────────────────────────────

def _make_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine


def _make_db(engine):
    Session = sessionmaker(bind=engine)
    return Session()


# ────────────────────────────────────────────────────────────────────────────────
# Helper: cria permissão + role + atribuição para um admin de tenant
# ────────────────────────────────────────────────────────────────────────────────

def _grant_tenant_admin_permission(db, user_id: str, tenant_id: str, perm_key: str, role_suffix: str = ""):
    """Cria a cadeia Permission -> Role -> RolePermission -> UserRoleAssignment.

    Permission e compartilhada por key (unique=True no modelo): cada perm_key
    produz no maximo 1 linha em permissions — reutilizada por todos os roles
    que a referenciam. Role e UserRoleAssignment sao sempre por usuario/tenant.
    """
    # Permission: upsert por key (a coluna key tem unique=True no modelo)
    perm_id = f"perm-{perm_key}"
    with db.no_autoflush:
        existing_perm = db.query(Permission).filter(Permission.key == perm_key).first()
        if existing_perm is None:
            db.add(Permission(id=perm_id, key=perm_key,
                              module=perm_key.split(".")[0], action=perm_key.split(".")[-1]))
        else:
            perm_id = existing_perm.id
    db.flush()

    # Role: um por (perm_key, role_suffix) — nome unico por contexto de uso
    role_suffix_key = role_suffix or tenant_id
    role_id = f"role-{perm_key}-{role_suffix_key}"
    with db.no_autoflush:
        if not db.get(Role, role_id):
            db.add(Role(id=role_id, name=f"role-{perm_key}-{role_suffix_key}", scope_type="tenant"))
    db.flush()

    # RolePermission: vincula role -> permission (respeita UniqueConstraint)
    existing_rp = db.query(RolePermission).filter(
        RolePermission.role_id == role_id,
        RolePermission.permission_id == perm_id,
    ).first()
    if not existing_rp:
        db.add(RolePermission(role_id=role_id, permission_id=perm_id))
    db.flush()

    # UserRoleAssignment: escopo do usuario ao seu tenant
    db.add(UserRoleAssignment(user_id=user_id, role_id=role_id, tenant_id=tenant_id))
    db.commit()


# ────────────────────────────────────────────────────────────────────────────────
# 1. ISOLAMENTO VIA /admin/tenants (camada HTTP)
# ────────────────────────────────────────────────────────────────────────────────

def _build_tenants_app(current_user_id: str):
    """Monta FastAPI mínimo com o router de tenants + banco isolado."""
    engine = _make_engine()
    db = _make_db(engine)

    db.add(Tenant(id=TENANT_A_ID, name="Alpha Co", slug="alpha-co", status="active", plan="business"))
    db.add(Tenant(id=TENANT_B_ID, name="Beta Co", slug="beta-co", status="active", plan="starter"))

    # super_admin — vê tudo
    db.add(User(id=SUPER_ADMIN_ID, email="sa@test.com", password_hash="x", role="super_admin"))
    # admin do tenant A
    db.add(User(id=ADMIN_A_ID, email="admin-a@test.com", password_hash="x", role="admin", tenant_id=TENANT_A_ID))
    # admin do tenant B
    db.add(User(id=ADMIN_B_ID, email="admin-b@test.com", password_hash="x", role="admin", tenant_id=TENANT_B_ID))
    db.commit()

    # Permissões RBAC para os admins de tenant
    _grant_tenant_admin_permission(db, ADMIN_A_ID, TENANT_A_ID, "tenants.read", "a")
    _grant_tenant_admin_permission(db, ADMIN_B_ID, TENANT_B_ID, "tenants.read", "b")

    test_app = FastAPI()
    test_app.include_router(tenants_router.router)
    test_app.dependency_overrides[get_db] = lambda: db
    test_app.dependency_overrides[get_current_user] = lambda: db.get(User, current_user_id)
    return TestClient(test_app), db


class TestTenantRouteIsolation:
    """Admin do tenant A nao pode ler/alterar o tenant B via /admin/tenants."""

    def test_admin_a_cannot_read_tenant_b(self):
        """GET /admin/tenants/{id} do tenant B retorna 404 para admin do tenant A."""
        client, _ = _build_tenants_app(ADMIN_A_ID)
        r = client.get(f"/admin/tenants/{TENANT_B_ID}")
        assert r.status_code == 404, (
            f"Esperado 404 mas recebeu {r.status_code}: {r.text}"
        )

    def test_admin_b_cannot_read_tenant_a(self):
        """GET /admin/tenants/{id} do tenant A retorna 404 para admin do tenant B."""
        client, _ = _build_tenants_app(ADMIN_B_ID)
        r = client.get(f"/admin/tenants/{TENANT_A_ID}")
        assert r.status_code == 404, (
            f"Esperado 404 mas recebeu {r.status_code}: {r.text}"
        )

    def test_admin_a_cannot_update_tenant_b(self):
        """PATCH /admin/tenants/{id} do tenant B retorna 404 para admin do tenant A.

        Cobre furo critico C13: tenant_admin alterando plano de concorrente.
        """
        client, db = _build_tenants_app(ADMIN_A_ID)
        r = client.patch(f"/admin/tenants/{TENANT_B_ID}", json={"plan": "enterprise"})
        assert r.status_code == 404, (
            f"Esperado 404 (bloqueio de isolamento) mas recebeu {r.status_code}: {r.text}"
        )
        # Verifica que nenhuma alteracao foi persistida
        db.expire_all()
        assert db.get(Tenant, TENANT_B_ID).plan == "starter", (
            "FURO: plano do tenant B foi alterado pelo admin do tenant A!"
        )

    def test_admin_a_list_shows_only_own_tenant(self):
        """GET /admin/tenants lista apenas o tenant A para o admin do tenant A."""
        client, _ = _build_tenants_app(ADMIN_A_ID)
        r = client.get("/admin/tenants")
        assert r.status_code == 200, r.text
        ids = {t["id"] for t in r.json()}
        assert TENANT_B_ID not in ids, (
            f"FURO: admin do tenant A ve o tenant B na listagem! ids={ids}"
        )
        assert TENANT_A_ID in ids, (
            f"Admin do tenant A nao ve seu proprio tenant na listagem. ids={ids}"
        )

    def test_super_admin_sees_all_tenants(self):
        """super_admin tem scope global e ve todos os tenants."""
        client, _ = _build_tenants_app(SUPER_ADMIN_ID)
        r = client.get("/admin/tenants")
        assert r.status_code == 200, r.text
        ids = {t["id"] for t in r.json()}
        assert {TENANT_A_ID, TENANT_B_ID} <= ids, (
            f"super_admin deveria ver todos os tenants, viu: {ids}"
        )

    def test_super_admin_can_read_any_tenant(self):
        """super_admin pode acessar o detalhe de qualquer tenant."""
        client, _ = _build_tenants_app(SUPER_ADMIN_ID)
        for tid in (TENANT_A_ID, TENANT_B_ID):
            r = client.get(f"/admin/tenants/{tid}")
            assert r.status_code == 200, f"super_admin deveria ver tenant {tid}: {r.text}"


# ────────────────────────────────────────────────────────────────────────────────
# 2. GUARD DE SAQUE B-02 (approve/reject_withdrawal cross-tenant)
# ────────────────────────────────────────────────────────────────────────────────

def _build_withdrawal_app(current_user_id: str):
    """Monta FastAPI mínimo com o router admin + saque do tenant B no banco."""
    engine = _make_engine()
    db = _make_db(engine)

    db.add(Tenant(id=TENANT_A_ID, name="Alpha Co", slug="alpha-co-w", status="active", plan="business"))
    db.add(Tenant(id=TENANT_B_ID, name="Beta Co", slug="beta-co-w", status="active", plan="business"))

    db.add(User(id=SUPER_ADMIN_ID, email="sa-w@test.com", password_hash="x", role="super_admin"))
    db.add(User(id=ADMIN_A_ID, email="admin-a-w@test.com", password_hash="x", role="admin", tenant_id=TENANT_A_ID))
    db.add(User(id=ADMIN_B_ID, email="admin-b-w@test.com", password_hash="x", role="admin", tenant_id=TENANT_B_ID))

    # Saque (provider="pix") pertencente ao tenant B
    db.add(Payment(
        id=PAYMENT_B_ID,
        tenant_id=TENANT_B_ID,
        tutor_id="walker-b",
        amount=100.0,
        status="pending",
        provider="pix",
    ))
    db.commit()

    # Permissão finance.manage para o admin A (para passar o RBAC e chegar no guard de tenant)
    _grant_tenant_admin_permission(db, ADMIN_A_ID, TENANT_A_ID, "finance.manage", "a-finance")
    # Permissão admin.access (exigida pelo router-level dependency)
    _grant_tenant_admin_permission(db, ADMIN_A_ID, TENANT_A_ID, "admin.access", "a-access")

    test_app = FastAPI()
    test_app.include_router(admin_router.router)
    test_app.dependency_overrides[get_db] = lambda: db
    test_app.dependency_overrides[get_current_user] = lambda: db.get(User, current_user_id)
    return TestClient(test_app), db


class TestWithdrawalGuardB02:
    """Guard de saque (B-02): admin do tenant A nao pode aprovar/rejeitar saque do tenant B."""

    def test_admin_a_cannot_approve_withdrawal_of_tenant_b(self):
        """POST /admin/withdrawals/{id}/approve retorna 404 se o saque pertence a outro tenant."""
        client, db = _build_withdrawal_app(ADMIN_A_ID)
        r = client.post(f"/admin/withdrawals/{PAYMENT_B_ID}/approve")
        assert r.status_code == 404, (
            f"FURO B-02: admin do tenant A aprovou saque do tenant B! status={r.status_code} body={r.text}"
        )
        # Confirma que o saque nao foi alterado
        db.expire_all()
        assert db.get(Payment, PAYMENT_B_ID).status == "pending", (
            "FURO B-02: status do saque foi alterado para 'paid' indevidamente!"
        )

    def test_admin_a_cannot_reject_withdrawal_of_tenant_b(self):
        """POST /admin/withdrawals/{id}/reject retorna 404 se o saque pertence a outro tenant."""
        client, db = _build_withdrawal_app(ADMIN_A_ID)
        r = client.post(f"/admin/withdrawals/{PAYMENT_B_ID}/reject")
        assert r.status_code == 404, (
            f"FURO B-02: admin do tenant A rejeitou saque do tenant B! status={r.status_code} body={r.text}"
        )
        db.expire_all()
        assert db.get(Payment, PAYMENT_B_ID).status == "pending", (
            "FURO B-02: status do saque foi alterado para 'rejected' indevidamente!"
        )

    def test_super_admin_can_approve_any_withdrawal(self):
        """super_admin (scope global) pode aprovar saque de qualquer tenant."""
        # Para este teste, o super_admin precisa de finance.manage + admin.access via role="super_admin"
        # (user_has_permission retorna True automaticamente para super_admin)
        engine = _make_engine()
        db = _make_db(engine)
        db.add(Tenant(id=TENANT_B_ID, name="Beta Co", slug="beta-co-sa", status="active", plan="business"))
        db.add(User(id=SUPER_ADMIN_ID, email="sa-sa@test.com", password_hash="x", role="super_admin"))
        db.add(Payment(
            id=PAYMENT_B_ID,
            tenant_id=TENANT_B_ID,
            tutor_id="walker-b",
            amount=100.0,
            status="pending",
            provider="pix",
        ))
        db.commit()

        test_app = FastAPI()
        test_app.include_router(admin_router.router)
        test_app.dependency_overrides[get_db] = lambda: db
        test_app.dependency_overrides[get_current_user] = lambda: db.get(User, SUPER_ADMIN_ID)

        client = TestClient(test_app)
        r = client.post(f"/admin/withdrawals/{PAYMENT_B_ID}/approve")
        assert r.status_code == 200, (
            f"super_admin deveria poder aprovar qualquer saque: {r.status_code} {r.text}"
        )
        db.expire_all()
        assert db.get(Payment, PAYMENT_B_ID).status == "paid"


# ────────────────────────────────────────────────────────────────────────────────
# 3. TESTES UNITARIOS DE apply_tenant_filter E ensure_tenant_access
# ────────────────────────────────────────────────────────────────────────────────

class FakeQuery:
    """Stub de SQLAlchemy Query que registra chamadas a .filter()."""

    def __init__(self, rows=None):
        self.filters = []
        self._rows = rows or []

    def filter(self, *criteria):
        self.filters.extend(criteria)
        return self

    def all(self):
        return self._rows


class ModelWithTenantId:
    tenant_id = sa_column("tenant_id")


class ModelWithoutTenantId:
    pass


def _make_scope(role: str, tenant_id: str | None, is_global: bool) -> AdminTenantScope:
    user = User(id=f"{role}-u", email=f"{role}@u.com", password_hash="x", role=role, tenant_id=tenant_id)
    return AdminTenantScope(user=user, tenant_id=tenant_id, is_global=is_global, role=role)


class TestApplyTenantFilter:
    """apply_tenant_filter: filtragem real por tenant_id quando scope nao e global."""

    def test_global_scope_does_not_add_filter(self):
        """Scope global (super_admin) nao adiciona filtro: query retorna inalterada."""
        query = FakeQuery()
        scope = _make_scope("super_admin", tenant_id=None, is_global=True)
        result = apply_tenant_filter(query, ModelWithTenantId, scope)
        assert result is query, "apply_tenant_filter deveria retornar a mesma query para scope global"
        assert query.filters == [], "Nenhum filtro deve ser adicionado para scope global"

    def test_non_global_scope_adds_tenant_id_filter(self):
        """Scope de tenant (admin) adiciona filtro tenant_id == scope.tenant_id."""
        query = FakeQuery()
        scope = _make_scope("admin", tenant_id="tenant-alpha", is_global=False)
        result = apply_tenant_filter(query, ModelWithTenantId, scope)
        assert result is query, "Deve retornar a mesma query (fluent interface)"
        assert len(query.filters) == 1, "Deve adicionar exatamente 1 filtro de tenant"

    def test_non_global_scope_with_model_without_tenant_id_raises_value_error(self):
        """Modelo sem tenant_id levanta ValueError quando scope nao e global."""
        import pytest
        query = FakeQuery()
        scope = _make_scope("admin", tenant_id="tenant-alpha", is_global=False)
        with pytest.raises(ValueError, match="tenant_id"):
            apply_tenant_filter(query, ModelWithoutTenantId, scope)

    def test_integration_with_real_db_two_tenants(self):
        """Teste de integracao real: apply_tenant_filter so retorna rows do tenant correto.

        Cria dois tenants no SQLite em memoria com passeios distintos e verifica
        que a query filtrada retorna apenas os registros do tenant escopo.
        """
        from datetime import datetime, timedelta, UTC

        engine = _make_engine()
        db = _make_db(engine)

        db.add(Tenant(id=TENANT_A_ID, name="A", slug="a-filter", status="active"))
        db.add(Tenant(id=TENANT_B_ID, name="B", slug="b-filter", status="active"))
        db.add(User(id="tutor-a", email="ta@t.com", password_hash="x", role="tutor", tenant_id=TENANT_A_ID))
        db.add(User(id="tutor-b", email="tb@t.com", password_hash="x", role="tutor", tenant_id=TENANT_B_ID))
        db.add(Pet(id="pet-a", tutor_id="tutor-a", tenant_id=TENANT_A_ID, name="Pet A", breed="SRD"))
        db.add(Pet(id="pet-b", tutor_id="tutor-b", tenant_id=TENANT_B_ID, name="Pet B", breed="SRD"))

        scheduled = (datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")
        db.add(Walk(id="walk-a", tutor_id="tutor-a", tenant_id=TENANT_A_ID, pet_id="pet-a",
                    scheduled_date=scheduled, duration_minutes=30, price=40.0, status="Agendado"))
        db.add(Walk(id="walk-b", tutor_id="tutor-b", tenant_id=TENANT_B_ID, pet_id="pet-b",
                    scheduled_date=scheduled, duration_minutes=30, price=40.0, status="Agendado"))
        db.commit()

        # Scope do tenant A — deve ver apenas walk-a
        scope_a = _make_scope("admin", tenant_id=TENANT_A_ID, is_global=False)
        base_query = db.query(Walk)
        filtered_a = apply_tenant_filter(base_query, Walk, scope_a).all()
        ids_a = {w.id for w in filtered_a}
        assert ids_a == {"walk-a"}, (
            f"FURO: apply_tenant_filter nao isolou por tenant. ids retornados: {ids_a}"
        )

        # Scope global (super_admin) — deve ver ambos
        scope_global = _make_scope("super_admin", tenant_id=None, is_global=True)
        filtered_global = apply_tenant_filter(db.query(Walk), Walk, scope_global).all()
        ids_global = {w.id for w in filtered_global}
        assert ids_global == {"walk-a", "walk-b"}, (
            f"Scope global deve retornar todos os walks. ids: {ids_global}"
        )

        db.close()


class TestEnsureTenantAccess:
    """ensure_tenant_access: bloqueia cross-tenant e permite mesmo tenant / global."""

    def test_same_tenant_allows_access(self):
        """Acesso ao proprio tenant nao levanta excecao."""
        scope = _make_scope("admin", tenant_id=TENANT_A_ID, is_global=False)
        result = ensure_tenant_access(TENANT_A_ID, scope)
        assert result is None  # retorno vazio = permitido

    def test_different_tenant_raises_404(self):
        """Acesso a tenant diferente levanta HTTPException 404."""
        import pytest
        from fastapi import HTTPException
        scope = _make_scope("admin", tenant_id=TENANT_A_ID, is_global=False)
        with pytest.raises(HTTPException) as exc_info:
            ensure_tenant_access(TENANT_B_ID, scope)
        assert exc_info.value.status_code == 404, (
            f"ensure_tenant_access deveria retornar 404, retornou {exc_info.value.status_code}"
        )

    def test_global_scope_bypasses_check(self):
        """Scope global (super_admin) sempre passa, independente do tenant do recurso."""
        scope = _make_scope("super_admin", tenant_id=None, is_global=True)
        # Nao deve levantar excecao
        result = ensure_tenant_access(TENANT_B_ID, scope)
        assert result is None

    def test_none_obj_tenant_raises_404_for_non_global(self):
        """Recurso sem tenant_id definido bloqueia acesso de admin de tenant."""
        import pytest
        from fastapi import HTTPException
        scope = _make_scope("admin", tenant_id=TENANT_A_ID, is_global=False)
        with pytest.raises(HTTPException) as exc_info:
            ensure_tenant_access(None, scope)
        assert exc_info.value.status_code == 404


# ────────────────────────────────────────────────────────────────────────────────
# 4. LISTAGEM DE WALKS: SO TRAZ LINHAS DO PROPRIO TENANT
# ────────────────────────────────────────────────────────────────────────────────

class TestWalkListingTenantIsolation:
    """Listagem de walks via list_walks() e via HTTP so retorna walks do proprio tenant."""

    def _setup_db_with_two_tenant_walks(self):
        from datetime import datetime, timedelta, UTC

        engine = _make_engine()
        db = _make_db(engine)

        db.add(Tenant(id=TENANT_A_ID, name="Alpha", slug="alpha-wl", status="active"))
        db.add(Tenant(id=TENANT_B_ID, name="Beta", slug="beta-wl", status="active"))

        db.add(User(id=ADMIN_A_ID, email="aa-wl@t.com", password_hash="x", role="admin", tenant_id=TENANT_A_ID))
        db.add(User(id=ADMIN_B_ID, email="ab-wl@t.com", password_hash="x", role="admin", tenant_id=TENANT_B_ID))
        db.add(User(id=SUPER_ADMIN_ID, email="sa-wl@t.com", password_hash="x", role="super_admin"))
        db.add(User(id="tutor-a-wl", email="ta-wl@t.com", password_hash="x", role="tutor", tenant_id=TENANT_A_ID))
        db.add(User(id="tutor-b-wl", email="tb-wl@t.com", password_hash="x", role="tutor", tenant_id=TENANT_B_ID))

        db.add(Pet(id="pet-a-wl", tutor_id="tutor-a-wl", tenant_id=TENANT_A_ID, name="Fido", breed="SRD"))
        db.add(Pet(id="pet-b-wl", tutor_id="tutor-b-wl", tenant_id=TENANT_B_ID, name="Rex", breed="SRD"))

        scheduled = (datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S")
        db.add(Walk(id="walk-a-wl", tutor_id="tutor-a-wl", tenant_id=TENANT_A_ID, pet_id="pet-a-wl",
                    scheduled_date=scheduled, duration_minutes=45, price=49.9, status="Agendado"))
        db.add(Walk(id="walk-b-wl", tutor_id="tutor-b-wl", tenant_id=TENANT_B_ID, pet_id="pet-b-wl",
                    scheduled_date=scheduled, duration_minutes=45, price=49.9, status="Agendado"))
        db.commit()
        return db

    def test_service_admin_a_only_sees_walk_a(self):
        """list_walks() com admin do tenant A so retorna walk-a (nao walk-b)."""
        from app.routes.walks import list_walks
        db = self._setup_db_with_two_tenant_walks()
        admin_a = db.get(User, ADMIN_A_ID)
        ids = {item["id"] for item in list_walks(user=admin_a, db=db, limit=50, full=False)}
        assert "walk-b-wl" not in ids, (
            f"FURO: admin do tenant A ve walk do tenant B! ids={ids}"
        )
        assert "walk-a-wl" in ids, "Admin A deve ver seu proprio walk."
        db.close()

    def test_service_admin_b_only_sees_walk_b(self):
        """list_walks() com admin do tenant B so retorna walk-b (nao walk-a)."""
        from app.routes.walks import list_walks
        db = self._setup_db_with_two_tenant_walks()
        admin_b = db.get(User, ADMIN_B_ID)
        ids = {item["id"] for item in list_walks(user=admin_b, db=db, limit=50, full=False)}
        assert "walk-a-wl" not in ids, (
            f"FURO: admin do tenant B ve walk do tenant A! ids={ids}"
        )
        assert "walk-b-wl" in ids, "Admin B deve ver seu proprio walk."
        db.close()

    def test_service_super_admin_sees_both_walks(self):
        """list_walks() com super_admin retorna walks de todos os tenants."""
        from app.routes.walks import list_walks
        db = self._setup_db_with_two_tenant_walks()
        sa = db.get(User, SUPER_ADMIN_ID)
        ids = {item["id"] for item in list_walks(user=sa, db=db, limit=50, full=False)}
        assert {"walk-a-wl", "walk-b-wl"} <= ids, (
            f"super_admin deve ver walks de todos os tenants. ids={ids}"
        )
        db.close()

    def test_service_admin_a_cannot_get_walk_b_by_id(self):
        """_get_walk_for_user() levanta 404 quando admin do tenant A acessa walk do tenant B."""
        import pytest
        from fastapi import HTTPException
        from app.routes.walks import _get_walk_for_user
        db = self._setup_db_with_two_tenant_walks()
        admin_a = db.get(User, ADMIN_A_ID)
        with pytest.raises(HTTPException) as exc_info:
            _get_walk_for_user("walk-b-wl", admin_a, db)
        assert exc_info.value.status_code == 404, (
            f"FURO: _get_walk_for_user deveria retornar 404 para walk de outro tenant"
        )
        db.close()
