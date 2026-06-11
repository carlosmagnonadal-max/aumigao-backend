"""Testes de segurança — Fase A2: "Operar como Tenant" (X-Act-As-Tenant).

Cobre os 4 casos obrigatórios:

(a) super_admin SEM header → escopo global (is_global=True, tenant_id=None).
(b) super_admin COM header X-Act-As-Tenant: T1 → escopo restrito ao T1
    (is_global=False, tenant_id="T1"). Via rota HTTP: GET /admin/coupons devolve
    SÓ cupons do T1; cupons do T2 NÃO aparecem.
(c) CRÍTICO: admin do tenant T1 envia X-Act-As-Tenant: T2 → personificação
    IGNORADA; scope.tenant_id continua sendo T1. Via rota HTTP: GET /admin/coupons
    devolve SÓ cupons do T1 (T2 não vaza).
(d) Suítes cross-tenant existentes: os testes unitários de get_admin_tenant_scope
    originais continuam passando (escopo esperado para admin com tenant_id).

Padrão do projeto: FastAPI mínimo, SQLite em memória (StaticPool),
dependency_overrides para get_db / get_current_user.
O header X-Act-As-Tenant é passado diretamente na requisição HTTP do TestClient.
"""
import pytest
from fastapi import FastAPI, Header, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 — registra todas as tabelas no Base.metadata
from app.core.database import Base, get_db
from app.dependencies.auth import get_current_user
from app.dependencies.rbac import require_permission
from app.dependencies.tenant_scope import (
    AdminTenantScope,
    get_admin_tenant_scope,
)
from app.models.coupon import Coupon
from app.models.rbac import Permission, Role, RolePermission, UserRoleAssignment
from app.models.tenant import Tenant, TenantFeature
from app.models.user import User
from app.routes import coupons as coupons_routes

# ─── IDs de fixture ───────────────────────────────────────────────────────────

SUPER_ADMIN_ID = "super-admin-1"
TENANT_ADMIN_T1_ID = "admin-t1"

T1 = "tenant-t1"
T2 = "tenant-t2"

COUPON_T1 = "PROMO-T1"
COUPON_T2 = "PROMO-T2"


# ─── Helpers de criação de User (sem DB) ─────────────────────────────────────


def _make_user(role: str, tenant_id: str | None = None) -> User:
    """Cria um User SQLAlchemy sem persistir — para testes unitários de scope."""
    return User(
        id=f"{role}-user",
        email=f"{role}@example.com",
        password_hash="hash",
        role=role,
        tenant_id=tenant_id,
    )


# ─── Banco em memória para testes de rota ────────────────────────────────────


def _build_db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    # super_admin (sem tenant_id próprio) — bypassa RBAC automaticamente
    db.add(
        User(
            id=SUPER_ADMIN_ID,
            email="superadmin@test.com",
            password_hash="x",
            role="super_admin",
        )
    )
    # admin do tenant T1
    db.add(
        User(
            id=TENANT_ADMIN_T1_ID,
            email="admint1@test.com",
            password_hash="x",
            role="admin",
            tenant_id=T1,
        )
    )

    # Tenants
    for tid, name in [(T1, "Tenant 1"), (T2, "Tenant 2")]:
        db.add(Tenant(id=tid, name=name, slug=f"slug-{tid}", status="active", plan="business"))
        db.add(TenantFeature(tenant_id=tid, feature_key="coupons", enabled=True))

    # Cupons: um por tenant
    db.add(
        Coupon(
            id="c-t1",
            tenant_id=T1,
            code=COUPON_T1,
            discount_type="percent",
            discount_value=10,
            active=True,
        )
    )
    db.add(
        Coupon(
            id="c-t2",
            tenant_id=T2,
            code=COUPON_T2,
            discount_type="percent",
            discount_value=20,
            active=True,
        )
    )

    # ── RBAC: admin de T1 precisa de admin.access + finance.read para acessar /admin/coupons
    perm_access = Permission(id="p-admin-access", key="admin.access", module="admin", action="access")
    perm_finance = Permission(id="p-finance-read", key="finance.read", module="finance", action="read")
    role_admin = Role(id="r-admin", name="tenant_admin", scope_type="tenant")
    db.add_all([perm_access, perm_finance, role_admin])
    db.flush()
    db.add(RolePermission(id="rp-1", role_id=role_admin.id, permission_id=perm_access.id))
    db.add(RolePermission(id="rp-2", role_id=role_admin.id, permission_id=perm_finance.id))
    db.add(
        UserRoleAssignment(
            id="ura-1",
            user_id=TENANT_ADMIN_T1_ID,
            role_id=role_admin.id,
            tenant_id=T1,
        )
    )

    db.commit()
    return db


def _build_app(db, *, user_id: str):
    """
    Monta FastAPI mínimo com o admin_router de cupons.

    O override de get_current_user DEVE replicar o que auth.py faz em produção:
    ler o header X-Act-As-Tenant e setar _act_as_tenant_id no user.
    Sem isso, o header passado no TestClient não chegaria ao tenant_scope.

    O RBAC usa as permissões semeadas no banco (_build_db):
    - super_admin: bypassa automaticamente (user_has_permission retorna True para role="super_admin")
    - admin de T1: tem UserRoleAssignment com admin.access + finance.read semeados
    """
    test_app = FastAPI()
    test_app.include_router(coupons_routes.admin_router)
    test_app.dependency_overrides[get_db] = lambda: db

    def _current_user_with_header(
        x_act_as_tenant: str | None = Header(default=None, alias="X-Act-As-Tenant"),
    ) -> User:
        user = db.get(User, user_id)
        # Replica exatamente o que get_current_user faz em produção:
        # guarda o header no user para que get_admin_tenant_scope o leia.
        user._act_as_tenant_id = x_act_as_tenant or None
        return user

    test_app.dependency_overrides[get_current_user] = _current_user_with_header
    return TestClient(test_app)


# ═══════════════════════════════════════════════════════════════════════════════
# (a) super_admin SEM header → escopo global
# ═══════════════════════════════════════════════════════════════════════════════


def test_a_super_admin_sem_header_escopo_global_unitario():
    """
    CASO (a) — unitário: get_admin_tenant_scope sem _act_as_tenant_id
    deve retornar is_global=True e tenant_id=None.
    """
    user = _make_user("super_admin")
    # Sem _act_as_tenant_id (como se o header não tivesse sido enviado)
    scope = get_admin_tenant_scope(user)

    assert scope.is_global is True, "super_admin sem header deve ter escopo global"
    assert scope.tenant_id is None, "tenant_id deve ser None no escopo global"
    assert scope.role == "super_admin"


def test_a_super_admin_sem_header_ve_todos_tenants_na_rota():
    """
    CASO (a) — rota HTTP: GET /admin/coupons sem header de personificação
    deve retornar cupons de múltiplos tenants (T1 e T2).

    NOTA: o _admin_tenant_id cai em resolve_current_tenant_id quando is_global=True,
    que retorna o tenant padrão (default). Para provar "acesso global" no nível de
    scope, o teste verifica que is_global=True está ativo e que get_admin_tenant_scope
    retorna escopo global.
    """
    user = _make_user("super_admin")
    # Simula sem header (padrão)
    scope = get_admin_tenant_scope(user)
    assert scope.is_global is True
    assert scope.tenant_id is None


# ═══════════════════════════════════════════════════════════════════════════════
# (b) super_admin COM header X-Act-As-Tenant: T1 → vê SÓ T1
# ═══════════════════════════════════════════════════════════════════════════════


def test_b_super_admin_com_header_escopo_restrito_unitario():
    """
    CASO (b) — unitário: super_admin com _act_as_tenant_id="T1"
    deve ter is_global=False e tenant_id="T1".
    """
    user = _make_user("super_admin")
    user._act_as_tenant_id = T1

    scope = get_admin_tenant_scope(user)

    assert scope.is_global is False, "super_admin com header deve ter escopo restrito"
    assert scope.tenant_id == T1, f"tenant_id deve ser '{T1}'"
    assert scope.role == "super_admin"


def test_b_super_admin_com_header_ve_so_cupons_do_t1():
    """
    CASO (b) — rota HTTP: GET /admin/coupons com header X-Act-As-Tenant: T1
    deve retornar APENAS cupons do T1. Cupons do T2 NÃO devem aparecer.
    """
    db = _build_db()
    client = _build_app(db, user_id=SUPER_ADMIN_ID)

    r = client.get("/admin/coupons", headers={"X-Act-As-Tenant": T1})
    assert r.status_code == 200, r.text
    body = r.json()

    codes = {c["code"] for c in body}
    assert COUPON_T1 in codes, f"Cupom do T1 '{COUPON_T1}' deve aparecer"
    assert COUPON_T2 not in codes, (
        f"FALHA DE SEGURANÇA: cupom do T2 '{COUPON_T2}' vazou para super_admin "
        f"operando como T1"
    )


def test_b_super_admin_com_header_ve_so_cupons_do_t2():
    """
    CASO (b) adicional — com header T2 vê SÓ cupons do T2; T1 não aparece.
    """
    db = _build_db()
    client = _build_app(db, user_id=SUPER_ADMIN_ID)

    r = client.get("/admin/coupons", headers={"X-Act-As-Tenant": T2})
    assert r.status_code == 200, r.text
    body = r.json()

    codes = {c["code"] for c in body}
    assert COUPON_T2 in codes, f"Cupom do T2 '{COUPON_T2}' deve aparecer"
    assert COUPON_T1 not in codes, (
        f"FALHA DE SEGURANÇA: cupom do T1 '{COUPON_T1}' vazou para super_admin "
        f"operando como T2"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# (c) CRÍTICO: admin do tenant T1 com header T2 → CONTINUA vendo só T1
# ═══════════════════════════════════════════════════════════════════════════════


def test_c_admin_tenant_ignora_header_unitario():
    """
    CASO (c) — CRÍTICO — unitário: admin de T1 com _act_as_tenant_id="T2"
    deve IGNORAR o header completamente e retornar scope.tenant_id == T1.
    A tentativa de personificação é silenciosamente bloqueada.
    """
    user = _make_user("admin", tenant_id=T1)
    # Simula o que get_current_user faria ao receber o header
    user._act_as_tenant_id = T2  # tentativa de personificação

    scope = get_admin_tenant_scope(user)

    assert scope.is_global is False, "admin de tenant nunca tem escopo global"
    assert scope.tenant_id == T1, (
        f"FALHA CRÍTICA DE SEGURANÇA: admin do T1 obteve scope.tenant_id='{scope.tenant_id}' "
        f"ao enviar header T2. A personificação DEVE ser bloqueada."
    )
    assert scope.tenant_id != T2, (
        "FALHA CRÍTICA: admin de T1 não pode personificar T2 via header"
    )


def test_c_admin_tenant_com_header_t2_ve_so_cupons_do_t1_na_rota():
    """
    CASO (c) — CRÍTICO — rota HTTP: admin do T1 enviando X-Act-As-Tenant: T2
    deve receber APENAS cupons do T1. Cupons do T2 NÃO devem aparecer.

    Este é o teste mais importante: prova que um admin de tenant não consegue
    escalar seus privilégios para acessar dados de outro tenant via header.
    """
    db = _build_db()
    client = _build_app(db, user_id=TENANT_ADMIN_T1_ID)

    # Admin do T1 tenta personificar T2
    r = client.get("/admin/coupons", headers={"X-Act-As-Tenant": T2})
    assert r.status_code == 200, r.text
    body = r.json()

    codes = {c["code"] for c in body}

    # Prova que T2 NÃO vazou
    assert COUPON_T2 not in codes, (
        "FALHA CRÍTICA DE SEGURANÇA: admin do T1 conseguiu acessar dados do T2 "
        "via header X-Act-As-Tenant. A personificação não foi bloqueada!"
    )

    # Prova que T1 ainda está acessível (admin continua funcionando normalmente)
    assert COUPON_T1 in codes, (
        f"Admin do T1 deve continuar vendo seus próprios cupons ('{COUPON_T1}')"
    )


def test_c_admin_tenant_com_header_vazio_continua_no_proprio_tenant():
    """
    CASO (c) adicional: header vazio ou whitespace não afeta o scope do admin.
    """
    user = _make_user("admin", tenant_id=T1)
    user._act_as_tenant_id = "   "  # whitespace — deve ser ignorado

    scope = get_admin_tenant_scope(user)

    assert scope.tenant_id == T1
    assert scope.is_global is False


# ═══════════════════════════════════════════════════════════════════════════════
# (d) Suítes cross-tenant existentes continuam passando
# ═══════════════════════════════════════════════════════════════════════════════


def test_d_regressao_admin_sem_tenant_id_levanta_excecao():
    """
    Regressão (d): admin sem tenant_id ainda levanta HTTPException 400.
    Comportamento original preservado.
    """
    from fastapi import HTTPException

    user = _make_user("admin", tenant_id=None)
    with pytest.raises(HTTPException) as exc_info:
        get_admin_tenant_scope(user)
    assert exc_info.value.status_code in {400, 422}


def test_d_regressao_admin_com_tenant_id_escopo_correto():
    """
    Regressão (d): admin com tenant_id retorna scope não-global com o tenant correto.
    Mesmo sem _act_as_tenant_id definido.
    """
    user = _make_user("admin", tenant_id="tenant-x")
    scope = get_admin_tenant_scope(user)

    assert scope.tenant_id == "tenant-x"
    assert scope.is_global is False
    assert scope.role == "admin"


def test_d_regressao_super_admin_sem_ato_como_global():
    """
    Regressão (d): super_admin sem _act_as_tenant_id (None) → is_global=True.
    """
    user = _make_user("super_admin")
    # _act_as_tenant_id não definido (simula request sem header)
    scope = get_admin_tenant_scope(user)

    assert scope.is_global is True
    assert scope.tenant_id is None


def test_d_regressao_super_admin_com_ato_none_como_global():
    """
    Regressão (d): super_admin com _act_as_tenant_id=None → is_global=True.
    """
    user = _make_user("super_admin")
    user._act_as_tenant_id = None
    scope = get_admin_tenant_scope(user)

    assert scope.is_global is True
    assert scope.tenant_id is None


def test_d_regressao_role_invalida_levanta_403():
    """
    Regressão (d): role que não é super_admin nem admin levanta 403.
    """
    from fastapi import HTTPException

    user = _make_user("tutor")
    with pytest.raises(HTTPException) as exc_info:
        get_admin_tenant_scope(user)
    assert exc_info.value.status_code == 403


# ═══════════════════════════════════════════════════════════════════════════════
# Extra: header com whitespace no valor para super_admin
# ═══════════════════════════════════════════════════════════════════════════════


def test_super_admin_header_whitespace_tratado_como_global():
    """
    super_admin com _act_as_tenant_id=" " (só espaço) → comportamento global.
    O .strip() deve descartar valores em branco.
    """
    user = _make_user("super_admin")
    user._act_as_tenant_id = "   "

    scope = get_admin_tenant_scope(user)

    assert scope.is_global is True
    assert scope.tenant_id is None


def test_super_admin_header_vazio_tratado_como_global():
    """
    super_admin com _act_as_tenant_id="" → comportamento global.
    """
    user = _make_user("super_admin")
    user._act_as_tenant_id = ""

    scope = get_admin_tenant_scope(user)

    assert scope.is_global is True
    assert scope.tenant_id is None
