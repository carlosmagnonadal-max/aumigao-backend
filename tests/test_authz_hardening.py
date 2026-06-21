"""Testes de ENDURECIMENTO DE AUTORIZAÇÃO (authorization hardening).

Cobre os bugs corrigidos nos EPICs 1, 2 e 3:

EPIC 1 — IDOR de ownership:
  IDOR-1: tutor A nao pode criar pagamento usando walk_id de tutor B (404).
  Ownership pets: tutor A nao pode GET/PUT/DELETE pet de tutor B (404).

EPIC 2 — Escopo de tenant em rotas admin:
  partner-applications/{id}: admin de tenant nao ve candidato de outro tenant.
  walker-kits/pending: requer permissao walkers.read (sem ela -> 403).
  walker-network (POST/PATCH): requer walkers.manage para escrita.
  referrals admin: requer referrals.manage para PATCH de status.
  notifications/seed-demo: tutor/walker recebe 403.
  notifications POST: admin de tenant nao cria notif para outro tenant.

Padrao: FastAPI minimo + SQLite em memoria (StaticPool) + overrides de get_db /
get_current_user. NUNCA importa app.main. super_admin bypassa todos os guards
(rede de seguranca do RBAC via user_has_permission).
"""
import re
from datetime import date

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
from app.models.rbac import Permission, Role, RolePermission, UserRoleAssignment
from app.models.tenant import Tenant
from app.models.user import User
from app.models.walk import Walk
from app.models.walker_referral import WalkerReferral
from app.routes import notifications, payments

# ---------------------------------------------------------------------------
# Constantes de IDs
# ---------------------------------------------------------------------------

TENANT_A = "tenant-a"
TENANT_B = "tenant-b"
TUTOR_A_ID = "tutor-a"
TUTOR_B_ID = "tutor-b"
SUPER_ADMIN_ID = "super-admin"
ADMIN_A_ID = "admin-tenant-a"
ADMIN_B_ID = "admin-tenant-b"
WALKER_ID = "walker-x"


# ---------------------------------------------------------------------------
# Helpers compartilhados
# ---------------------------------------------------------------------------


def _new_db():
    """Cria um novo SQLite em memoria isolado e retorna a sessao."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()


def _phone_normalize(phone: str) -> str:
    return re.sub(r"\D", "", phone)


def _grant_permission(db, user_id: str, tenant_id: str, *perm_keys: str) -> None:
    """Semeia a cadeia Permission -> Role -> RolePermission -> UserRoleAssignment.

    Replica o padrao de test_multitenant_isolation.py para evitar falhas de
    FK ao usar require_permission() com usuario nao-super_admin.

    Aceita uma ou mais perm_keys para serem concedidas numa unica chamada.
    """
    for perm_key in perm_keys:
        perm_id = f"perm-{perm_key}"
        with db.no_autoflush:
            existing_perm = db.query(Permission).filter(Permission.key == perm_key).first()
            if existing_perm is None:
                db.add(Permission(id=perm_id, key=perm_key,
                                  module=perm_key.split(".")[0],
                                  action=perm_key.split(".")[-1]))
            else:
                perm_id = existing_perm.id
        db.flush()

        role_id = f"role-{perm_key}-{tenant_id}-{user_id}"
        with db.no_autoflush:
            if not db.get(Role, role_id):
                db.add(Role(id=role_id, name=role_id, scope_type="tenant"))
        db.flush()

        existing_rp = db.query(RolePermission).filter(
            RolePermission.role_id == role_id,
            RolePermission.permission_id == perm_id,
        ).first()
        if not existing_rp:
            db.add(RolePermission(role_id=role_id, permission_id=perm_id))
        db.flush()

        db.add(UserRoleAssignment(user_id=user_id, role_id=role_id, tenant_id=tenant_id))

    db.commit()


def _make_walk(db, walk_id: str, tutor_id: str, pet_id: str, tenant_id: str) -> Walk:
    """Cria uma Walk com todos os campos NOT NULL obrigatorios."""
    walk = Walk(
        id=walk_id,
        tutor_id=tutor_id,
        tenant_id=tenant_id,
        pet_id=pet_id,
        scheduled_date=date(2026, 7, 1),
        duration_minutes=30,
        price=50.0,
        status="Agendado",
        operational_status="pending_walker_confirmation",
    )
    db.add(walk)
    return walk


def _make_referral(db, referral_id: str, referrer_id: str) -> WalkerReferral:
    """Cria uma WalkerReferral com todos os campos NOT NULL obrigatorios."""
    phone = "11999999999"
    referral = WalkerReferral(
        id=referral_id,
        referrer_user_id=referrer_id,
        referred_name="Fulano",
        referred_phone=phone,
        referred_phone_normalized=_phone_normalize(phone),
        city="Salvador",
        neighborhood="Pituba",
        referral_code="AUM-TEST1",
        invite_link="http://x/AUM-TEST1",
        status="pending",
        reward_status="not_eligible",
    )
    db.add(referral)
    return referral


# ---------------------------------------------------------------------------
# EPIC 1 — IDOR de ownership: payments.py create_payment
# ---------------------------------------------------------------------------


class TestIDOR1CreatePaymentOwnership:
    """IDOR-1: tutor A nao pode criar pagamento com walk_id que pertence a tutor B."""

    def _build(self):
        db = _new_db()
        db.add(Tenant(id=TENANT_A, name="A", slug="tenant-a", status="active", plan="starter"))
        tutor_a = User(id=TUTOR_A_ID, email="a@test.com", password_hash="x",
                       role="cliente", tenant_id=TENANT_A)
        tutor_b = User(id=TUTOR_B_ID, email="b@test.com", password_hash="x",
                       role="cliente", tenant_id=TENANT_A)
        db.add_all([tutor_a, tutor_b])
        pet_b = Pet(id="pet-b", tutor_id=TUTOR_B_ID, tenant_id=TENANT_A,
                    name="Rex", species="cachorro")
        db.add(pet_b)
        db.flush()
        _make_walk(db, "walk-b", TUTOR_B_ID, "pet-b", TENANT_A)
        db.commit()

        app_ = FastAPI()
        app_.include_router(payments.router)
        app_.dependency_overrides[get_db] = lambda: db
        return app_, db

    def test_tutor_a_cannot_create_payment_for_walk_of_tutor_b(self, monkeypatch):
        """Tutor A tenta pagar walk de tutor B -> 404 (IDOR bloqueado)."""
        monkeypatch.setattr(payments, "PAYMENT_MODE", "asaas_sandbox")
        app_, db = self._build()
        app_.dependency_overrides[get_current_user] = lambda: db.get(User, TUTOR_A_ID)
        client = TestClient(app_)
        r = client.post("/payments/create", json={"amount": 50.0, "method": "pix", "walk_id": "walk-b"})
        assert r.status_code == 404, r.text
        assert "nao encontrado" in r.json()["detail"].lower()

    def test_tutor_b_can_create_payment_for_own_walk(self, monkeypatch):
        """Tutor B (dono do walk) consegue criar pagamento normalmente."""
        monkeypatch.setattr(payments, "PAYMENT_MODE", "asaas_sandbox")

        async def _fake_asaas(payload, user):
            return (
                {"id": "asaas-1", "status": "PENDING", "invoiceUrl": None, "bankSlipUrl": None},
                {},
                "PIX",
            )

        monkeypatch.setattr(payments, "create_asaas_payment", _fake_asaas)
        app_, db = self._build()
        app_.dependency_overrides[get_current_user] = lambda: db.get(User, TUTOR_B_ID)
        client = TestClient(app_)
        r = client.post("/payments/create", json={"amount": 50.0, "method": "pix", "walk_id": "walk-b"})
        # Deve chegar no processamento (nao 404 de IDOR)
        assert r.status_code != 404, r.text

    def test_walk_id_none_does_not_trigger_idor_check(self, monkeypatch):
        """Sem walk_id (pagamento avulso) o check de IDOR nao e ativado."""
        monkeypatch.setattr(payments, "PAYMENT_MODE", "asaas_sandbox")

        async def _fake_asaas(payload, user):
            return (
                {"id": "asaas-2", "status": "PENDING", "invoiceUrl": None, "bankSlipUrl": None},
                {},
                "PIX",
            )

        monkeypatch.setattr(payments, "create_asaas_payment", _fake_asaas)
        app_, db = self._build()
        app_.dependency_overrides[get_current_user] = lambda: db.get(User, TUTOR_A_ID)
        client = TestClient(app_)
        r = client.post("/payments/create", json={"amount": 50.0, "method": "pix"})
        # Nao deve retornar 404 de IDOR
        assert r.status_code != 404, r.text

    def test_unknown_walk_id_is_treated_as_idempotency_key(self, monkeypatch):
        """Walk inexistente nao e bloqueado — trata-se como chave de idempotencia legada.

        O IDOR check so bloqueia quando o Walk EXISTE no banco com outro tutor_id.
        """
        monkeypatch.setattr(payments, "PAYMENT_MODE", "asaas_sandbox")

        async def _fake_asaas(payload, user):
            return (
                {"id": "asaas-3", "status": "PENDING", "invoiceUrl": None, "bankSlipUrl": None},
                {},
                "PIX",
            )

        monkeypatch.setattr(payments, "create_asaas_payment", _fake_asaas)
        app_, db = self._build()
        app_.dependency_overrides[get_current_user] = lambda: db.get(User, TUTOR_A_ID)
        client = TestClient(app_)
        r = client.post("/payments/create", json={"amount": 50.0, "method": "pix", "walk_id": "nope"})
        # Walk nao existe no banco → nao e bloqueado pelo IDOR check (walk_id = idempotency key)
        assert r.status_code != 404, r.text


# ---------------------------------------------------------------------------
# EPIC 1 — Ownership em pets.py
# ---------------------------------------------------------------------------


class TestOwnershipPets:
    """Tutor A nao pode GET/PUT/DELETE pet que pertence a tutor B."""

    def _build(self):
        from app.routes import pets as pets_mod
        db = _new_db()
        db.add(Tenant(id=TENANT_A, name="A", slug="tenant-a", status="active", plan="starter"))
        db.add(User(id=TUTOR_A_ID, email="a@test.com", password_hash="x",
                    role="cliente", tenant_id=TENANT_A))
        db.add(User(id=TUTOR_B_ID, email="b@test.com", password_hash="x",
                    role="cliente", tenant_id=TENANT_A))
        pet_b = Pet(id="pet-b", tutor_id=TUTOR_B_ID, tenant_id=TENANT_A,
                    name="Rex", species="cachorro")
        db.add(pet_b)
        db.commit()

        app_ = FastAPI()
        app_.include_router(pets_mod.router)
        app_.dependency_overrides[get_db] = lambda: db
        app_.dependency_overrides[get_current_user] = lambda: db.get(User, TUTOR_A_ID)
        return TestClient(app_), db

    def test_tutor_a_cannot_get_pet_of_tutor_b(self):
        client, _ = self._build()
        r = client.get("/pets/pet-b")
        assert r.status_code == 404, r.text

    def test_tutor_a_cannot_put_pet_of_tutor_b(self):
        client, _ = self._build()
        r = client.put("/pets/pet-b", json={"name": "Hacked"})
        assert r.status_code == 404, r.text

    def test_tutor_a_cannot_delete_pet_of_tutor_b(self):
        client, _ = self._build()
        r = client.delete("/pets/pet-b")
        assert r.status_code == 404, r.text

    def test_tutor_b_can_get_own_pet(self):
        client, db = self._build()
        client.app.dependency_overrides[get_current_user] = lambda: db.get(User, TUTOR_B_ID)
        r = client.get("/pets/pet-b")
        assert r.status_code == 200, r.text

    def test_tutor_b_can_update_own_pet(self):
        client, db = self._build()
        client.app.dependency_overrides[get_current_user] = lambda: db.get(User, TUTOR_B_ID)
        r = client.put("/pets/pet-b", json={"name": "NewName"})
        assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# EPIC 2 — walker-kits/pending: requer permissao walkers.read
# ---------------------------------------------------------------------------


class TestWalkerKitsPending:
    """GET /admin/walker-kits/pending deve exigir permissao walkers.read."""

    def _build_as(self, role: str, user_id: str, grant_perm: bool = False):
        from app.routes import admin as admin_mod
        db = _new_db()
        db.add(Tenant(id=TENANT_A, name="A", slug="tenant-a", status="active", plan="starter"))
        user = User(id=user_id, email=f"{user_id}@test.com", password_hash="x",
                    role=role, tenant_id=TENANT_A)
        db.add(user)
        db.commit()
        if grant_perm:
            # admin router requer admin.access no nivel do router + walkers.read na rota
            _grant_permission(db, user_id, TENANT_A, "admin.access", "walkers.read")

        app_ = FastAPI()
        app_.include_router(admin_mod.router)
        app_.dependency_overrides[get_db] = lambda: db
        app_.dependency_overrides[get_current_user] = lambda: db.get(User, user_id)
        return TestClient(app_)

    def test_super_admin_can_list_pending_kits(self):
        client = self._build_as("super_admin", SUPER_ADMIN_ID)
        r = client.get("/admin/walker-kits/pending")
        assert r.status_code == 200, r.text
        data = r.json()
        assert "items" in data
        assert "total" in data

    def test_admin_with_perm_can_list_pending_kits(self):
        client = self._build_as("admin", ADMIN_A_ID, grant_perm=True)
        r = client.get("/admin/walker-kits/pending")
        assert r.status_code == 200, r.text

    def test_tutor_cannot_list_pending_kits(self):
        """Tutor nao tem walkers.read — deve receber 403."""
        client = self._build_as("cliente", TUTOR_A_ID)
        r = client.get("/admin/walker-kits/pending")
        assert r.status_code == 403, r.text


# ---------------------------------------------------------------------------
# EPIC 2 — partner-applications/{id}: scope de tenant
# ---------------------------------------------------------------------------


class TestPartnerApplicationDetail:
    """Admin de tenant A nao pode ver candidatura de walker do tenant B."""

    def _build(self):
        from app.routes import admin as admin_mod
        from app.models.walker_profile import WalkerProfile
        db = _new_db()

        db.add(Tenant(id=TENANT_A, name="A", slug="tenant-a", status="active", plan="starter"))
        db.add(Tenant(id=TENANT_B, name="B", slug="tenant-b", status="active", plan="starter"))

        walker_b_user = User(id="walker-b-user", email="wb@test.com",
                              password_hash="x", role="walker", tenant_id=TENANT_B)
        walker_a_user = User(id="walker-a-user", email="wa@test.com",
                              password_hash="x", role="walker", tenant_id=TENANT_A)
        admin_a = User(id=ADMIN_A_ID, email="aa@test.com", password_hash="x",
                        role="admin", tenant_id=TENANT_A)
        super_admin = User(id=SUPER_ADMIN_ID, email="sa@test.com", password_hash="x",
                            role="super_admin", tenant_id=TENANT_A)
        db.add_all([walker_b_user, walker_a_user, admin_a, super_admin])
        db.flush()

        profile_b = WalkerProfile(id="profile-b", user_id="walker-b-user", status="pending")
        profile_a = WalkerProfile(id="profile-a", user_id="walker-a-user", status="pending")
        db.add_all([profile_b, profile_a])
        db.commit()

        # admin router requer admin.access no nivel do router + walkers.read na rota
        _grant_permission(db, ADMIN_A_ID, TENANT_A, "admin.access", "walkers.read")

        app_ = FastAPI()
        app_.include_router(admin_mod.router)
        app_.dependency_overrides[get_db] = lambda: db
        return app_, db

    def test_admin_tenant_a_cannot_see_profile_of_tenant_b(self):
        """Admin do tenant A ve perfil de walker do tenant B -> 404."""
        app_, db = self._build()
        app_.dependency_overrides[get_current_user] = lambda: db.get(User, ADMIN_A_ID)
        client = TestClient(app_)
        r = client.get("/admin/partner-applications/profile-b")
        assert r.status_code == 404, r.text

    def test_admin_tenant_a_can_see_profile_of_tenant_a(self):
        """Admin do tenant A ve perfil de walker do proprio tenant -> 200."""
        app_, db = self._build()
        app_.dependency_overrides[get_current_user] = lambda: db.get(User, ADMIN_A_ID)
        client = TestClient(app_)
        r = client.get("/admin/partner-applications/profile-a")
        assert r.status_code == 200, r.text

    def test_super_admin_can_see_any_profile(self):
        """Super admin ve qualquer perfil independente de tenant."""
        app_, db = self._build()
        app_.dependency_overrides[get_current_user] = lambda: db.get(User, SUPER_ADMIN_ID)
        client = TestClient(app_)
        r = client.get("/admin/partner-applications/profile-b")
        assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# EPIC 2 — walker_network: escrita exige walkers.manage (nao apenas walkers.read)
# ---------------------------------------------------------------------------


class TestWalkerNetworkWritePermission:
    """POST e PATCH na rede de passeadores devem exigir walkers.manage.

    super_admin tem todos os privilegios (bypass do RBAC).
    Tutor/cliente -> 403 nas rotas de escrita.
    Admin de tenant A -> 404 ao tentar operar no tenant B (scope check).
    """

    def _build_db(self, plan: str = "business") -> tuple:
        from app.routes import walker_network as wn_mod
        db = _new_db()
        db.add(Tenant(id=TENANT_A, name="A", slug="tenant-a", status="active", plan=plan))
        super_admin = User(id=SUPER_ADMIN_ID, email="sa@test.com", password_hash="x",
                            role="super_admin", tenant_id=TENANT_A)
        tutor = User(id=TUTOR_A_ID, email="t@test.com", password_hash="x",
                      role="cliente", tenant_id=TENANT_A)
        walker = User(id=WALKER_ID, email="w@test.com", password_hash="x",
                       role="walker", tenant_id=TENANT_A)
        admin_a = User(id=ADMIN_A_ID, email="aa@test.com", password_hash="x",
                        role="admin", tenant_id=TENANT_A)
        db.add_all([super_admin, tutor, walker, admin_a])
        db.commit()

        app_ = FastAPI()
        app_.include_router(wn_mod.router)
        app_.dependency_overrides[get_db] = lambda: db
        return app_, db

    def test_tutor_cannot_link_walker_to_tenant(self):
        """Tutor nao tem walkers.manage -> 403."""
        app_, db = self._build_db()
        app_.dependency_overrides[get_current_user] = lambda: db.get(User, TUTOR_A_ID)
        client = TestClient(app_)
        r = client.post(f"/admin/walker-network/tenants/{TENANT_A}",
                        json={"walker_user_id": WALKER_ID, "access_type": "shared_network", "status": "pending"})
        assert r.status_code == 403, r.text

    def test_tutor_cannot_patch_walker_access(self):
        """Tutor nao tem walkers.manage -> 403."""
        app_, db = self._build_db()
        app_.dependency_overrides[get_current_user] = lambda: db.get(User, TUTOR_A_ID)
        client = TestClient(app_)
        r = client.patch(f"/admin/walker-network/tenants/{TENANT_A}/walkers/{WALKER_ID}",
                         json={"status": "inactive"})
        assert r.status_code == 403, r.text

    def test_super_admin_can_link_walker_to_tenant(self):
        """super_admin bypassa o guard e consegue vincular walker -> 200."""
        app_, db = self._build_db(plan="business")
        app_.dependency_overrides[get_current_user] = lambda: db.get(User, SUPER_ADMIN_ID)
        client = TestClient(app_)
        r = client.post(f"/admin/walker-network/tenants/{TENANT_A}",
                        json={"walker_user_id": WALKER_ID, "access_type": "shared_network", "status": "pending"})
        assert r.status_code == 200, r.text

    def test_admin_tenant_a_cannot_link_walker_to_tenant_b(self):
        """Admin do tenant A nao pode vincular walker ao tenant B (scope check -> 404)."""
        from app.routes import walker_network as wn_mod
        db = _new_db()
        db.add(Tenant(id=TENANT_A, name="A", slug="tenant-a", status="active", plan="business"))
        db.add(Tenant(id=TENANT_B, name="B", slug="tenant-b", status="active", plan="business"))
        admin_a = User(id=ADMIN_A_ID, email="aa@test.com", password_hash="x",
                        role="admin", tenant_id=TENANT_A)
        walker = User(id=WALKER_ID, email="w@test.com", password_hash="x",
                       role="walker", tenant_id=TENANT_A)
        db.add_all([admin_a, walker])
        db.commit()
        # walker_network router requer walkers.read no nivel do router + walkers.manage na rota
        _grant_permission(db, ADMIN_A_ID, TENANT_A, "walkers.read", "walkers.manage")

        app_ = FastAPI()
        app_.include_router(wn_mod.router)
        app_.dependency_overrides[get_db] = lambda: db
        app_.dependency_overrides[get_current_user] = lambda: db.get(User, ADMIN_A_ID)
        client = TestClient(app_)
        r = client.post(f"/admin/walker-network/tenants/{TENANT_B}",
                        json={"walker_user_id": WALKER_ID, "access_type": "shared_network", "status": "pending"})
        assert r.status_code == 404, r.text


# ---------------------------------------------------------------------------
# EPIC 2 — referrals: PATCH status exige referrals.manage
# ---------------------------------------------------------------------------


class TestReferralsWritePermission:
    """PATCH /admin/referrals/walkers/{id}/status deve exigir referrals.manage."""

    def _build(self):
        from app.routes import referrals as ref_mod
        db = _new_db()
        db.add(Tenant(id=TENANT_A, name="A", slug="tenant-a", status="active", plan="starter"))
        super_admin = User(id=SUPER_ADMIN_ID, email="sa@test.com", password_hash="x",
                            role="super_admin")
        tutor = User(id=TUTOR_A_ID, email="t@test.com", password_hash="x",
                      role="cliente", tenant_id=TENANT_A)
        referrer = User(id="referrer-1", email="ref@test.com", password_hash="x",
                         role="cliente", tenant_id=TENANT_A)
        db.add_all([super_admin, tutor, referrer])
        db.flush()
        _make_referral(db, "ref-1", "referrer-1")
        db.commit()

        app_ = FastAPI()
        app_.include_router(ref_mod.admin_router)
        app_.include_router(ref_mod.api_admin_router)
        app_.dependency_overrides[get_db] = lambda: db
        return app_, db

    def test_tutor_cannot_patch_referral_status(self):
        """Tutor nao tem referrals.manage -> 403."""
        app_, db = self._build()
        app_.dependency_overrides[get_current_user] = lambda: db.get(User, TUTOR_A_ID)
        client = TestClient(app_)
        r = client.patch("/admin/referrals/walkers/ref-1/status",
                         json={"status": "approved"})
        assert r.status_code == 403, r.text

    def test_super_admin_can_patch_referral_status(self):
        """super_admin bypassa o guard -> 200."""
        app_, db = self._build()
        app_.dependency_overrides[get_current_user] = lambda: db.get(User, SUPER_ADMIN_ID)
        client = TestClient(app_)
        r = client.patch("/admin/referrals/walkers/ref-1/status",
                         json={"status": "approved"})
        assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# EPIC 2 — notifications/seed-demo: restrito a admin
# ---------------------------------------------------------------------------


class TestNotificationsSeedDemo:
    """POST /notifications/seed-demo deve recusar tutores e walkers."""

    def _build(self):
        db = _new_db()
        db.add(Tenant(id=TENANT_A, name="A", slug="tenant-a", status="active", plan="starter"))
        tutor = User(id=TUTOR_A_ID, email="t@test.com", password_hash="x",
                      role="cliente", tenant_id=TENANT_A)
        walker = User(id=WALKER_ID, email="w@test.com", password_hash="x",
                       role="walker", tenant_id=TENANT_A)
        admin = User(id=SUPER_ADMIN_ID, email="sa@test.com", password_hash="x",
                      role="super_admin", tenant_id=TENANT_A)
        db.add_all([tutor, walker, admin])
        db.commit()

        app_ = FastAPI()
        app_.include_router(notifications.router)
        app_.dependency_overrides[get_db] = lambda: db
        return app_, db

    def test_tutor_cannot_seed_demo_notifications(self):
        app_, db = self._build()
        app_.dependency_overrides[get_current_user] = lambda: db.get(User, TUTOR_A_ID)
        client = TestClient(app_)
        r = client.post("/notifications/seed-demo")
        assert r.status_code == 403, r.text

    def test_walker_cannot_seed_demo_notifications(self):
        app_, db = self._build()
        app_.dependency_overrides[get_current_user] = lambda: db.get(User, WALKER_ID)
        client = TestClient(app_)
        r = client.post("/notifications/seed-demo")
        assert r.status_code == 403, r.text

    def test_admin_can_seed_demo_notifications(self):
        app_, db = self._build()
        app_.dependency_overrides[get_current_user] = lambda: db.get(User, SUPER_ADMIN_ID)
        client = TestClient(app_)
        r = client.post("/notifications/seed-demo")
        assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# EPIC 2 — notifications POST: admin de tenant nao cria notif para outro tenant
# ---------------------------------------------------------------------------


class TestNotificationsCreateTenantIsolation:
    """POST /notifications deve bloquear admin de tenant A criar notif para tenant B."""

    def _build(self):
        db = _new_db()
        db.add(Tenant(id=TENANT_A, name="A", slug="tenant-a", status="active", plan="starter"))
        db.add(Tenant(id=TENANT_B, name="B", slug="tenant-b", status="active", plan="starter"))
        admin_a = User(id=ADMIN_A_ID, email="aa@test.com", password_hash="x",
                        role="admin", tenant_id=TENANT_A)
        super_admin = User(id=SUPER_ADMIN_ID, email="sa@test.com", password_hash="x",
                            role="super_admin", tenant_id=TENANT_A)
        target_user = User(id="target-b", email="tb@test.com", password_hash="x",
                            role="cliente", tenant_id=TENANT_B)
        db.add_all([admin_a, super_admin, target_user])
        db.commit()

        app_ = FastAPI()
        app_.include_router(notifications.router)
        app_.dependency_overrides[get_db] = lambda: db
        return app_, db

    def test_admin_a_cannot_create_notification_for_tenant_b(self):
        """Admin do tenant A tenta criar notif para tenant B -> 403."""
        app_, db = self._build()
        app_.dependency_overrides[get_current_user] = lambda: db.get(User, ADMIN_A_ID)
        client = TestClient(app_)
        r = client.post("/notifications", json={
            "tenant_id": TENANT_B,
            "user_id": "target-b",
            "user_role": "tutor",
            "title": "Test",
            "message": "Hacked",
        })
        assert r.status_code == 403, r.text

    def test_super_admin_can_create_notification_for_any_tenant(self):
        """super_admin cria notif para qualquer tenant -> 200."""
        app_, db = self._build()
        app_.dependency_overrides[get_current_user] = lambda: db.get(User, SUPER_ADMIN_ID)
        client = TestClient(app_)
        r = client.post("/notifications", json={
            "tenant_id": TENANT_B,
            "user_id": "target-b",
            "user_role": "tutor",
            "title": "Test",
            "message": "From super_admin",
        })
        assert r.status_code == 200, r.text

    def test_tutor_cannot_create_notification(self):
        """Tutor nao tem permissao de admin -> 403."""
        db = _new_db()
        db.add(Tenant(id=TENANT_A, name="A", slug="tenant-a", status="active", plan="starter"))
        db.add(User(id=TUTOR_A_ID, email="t@test.com", password_hash="x",
                     role="cliente", tenant_id=TENANT_A))
        db.commit()

        app_ = FastAPI()
        app_.include_router(notifications.router)
        app_.dependency_overrides[get_db] = lambda: db
        app_.dependency_overrides[get_current_user] = lambda: db.get(User, TUTOR_A_ID)
        client = TestClient(app_)
        r = client.post("/notifications", json={
            "title": "Test",
            "message": "Should fail",
        })
        assert r.status_code == 403, r.text
