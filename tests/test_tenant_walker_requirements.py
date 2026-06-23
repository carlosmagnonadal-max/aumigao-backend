"""TDD — background-check extra por tenant (F3.2).

Task 2: gate requirements_met na elegibilidade.
Task 3: vínculo novo a tenant-com-requisitos nasce pendente + helper initial_requirements_met.
"""
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.models.user import User
from app.models.walker_profile import WalkerProfile
from app.models.tenant import Tenant
from app.models.tenant_walker_access import TenantWalkerAccess
from app.services.walker_network_matching_service import (
    get_tenant_eligible_walker_ids,
    initial_requirements_met,
    is_walker_eligible_for_tenant,
)


def _db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _seed_walker(db, walker_id="w1"):
    db.add(User(id=walker_id, email=f"{walker_id}@t.invalid", password_hash="x",
                role="walker", is_active=True, token_version=0, must_change_password=False))
    db.add(WalkerProfile(id=f"p-{walker_id}", user_id=walker_id, status="active", active_as_walker=True))
    db.commit()


def _seed_tenant(db, tenant_id="tA", requirements=None):
    db.add(Tenant(id=tenant_id, name=f"Tenant {tenant_id}", slug=f"tenant-{tenant_id}",
                  walker_extra_requirements=requirements))
    db.commit()


def _link(db, walker_id, tenant_id, status="active", requirements_met=True):
    db.add(TenantWalkerAccess(
        id=str(uuid4()), tenant_id=tenant_id, walker_user_id=walker_id,
        status=status, access_type="shared_network", requirements_met=requirements_met,
    ))
    db.commit()


# ── Task 2: gate ────────────────────────────────────────────────────────────

def test_walker_com_requirements_nao_met_fica_fora_do_pool():
    db = _db()
    _seed_tenant(db, "tA")
    _seed_walker(db, "w1")
    _link(db, "w1", "tA", requirements_met=False)
    assert "w1" not in get_tenant_eligible_walker_ids(db, "tA")
    assert is_walker_eligible_for_tenant(db, "tA", "w1") is False


def test_walker_com_requirements_met_entra_no_pool():
    db = _db()
    _seed_tenant(db, "tA")
    _seed_walker(db, "w1")
    _link(db, "w1", "tA", requirements_met=True)
    assert "w1" in get_tenant_eligible_walker_ids(db, "tA")
    assert is_walker_eligible_for_tenant(db, "tA", "w1") is True


# ── Task 3: helper + vínculo novo pendente ──────────────────────────────────

def test_initial_requirements_met_helper():
    db = _db()
    _seed_tenant(db, "tCom", requirements=["Curso"])
    _seed_tenant(db, "tSem", requirements=None)
    _seed_tenant(db, "tVazio", requirements=[])
    assert initial_requirements_met(db, "tCom") is False  # tem requisito → pendente
    assert initial_requirements_met(db, "tSem") is True    # sem requisito → ativo
    assert initial_requirements_met(db, "tVazio") is True  # lista vazia → ativo


def test_aceitar_convite_de_tenant_com_requisitos_fica_pendente():
    from app.routes.walker_network import _respond_to_invite
    db = _db()
    _seed_tenant(db, "tA", requirements=["Curso de primeiros socorros"])
    _seed_walker(db, "w1")
    invite = TenantWalkerAccess(id="inv1", tenant_id="tA", walker_user_id="w1",
                               status="pending", access_type="shared_network")
    db.add(invite)
    db.commit()
    user = db.get(User, "w1")
    _respond_to_invite("inv1", "active", user, db)
    refreshed = db.get(TenantWalkerAccess, "inv1")
    assert refreshed.status == "active"
    assert refreshed.requirements_met is False  # tenant tem requisitos → pendente


def test_aceitar_convite_de_tenant_sem_requisitos_fica_ativo():
    from app.routes.walker_network import _respond_to_invite
    db = _db()
    _seed_tenant(db, "tB", requirements=None)
    _seed_walker(db, "w2")
    invite = TenantWalkerAccess(id="inv2", tenant_id="tB", walker_user_id="w2",
                               status="pending", access_type="shared_network")
    db.add(invite)
    db.commit()
    user = db.get(User, "w2")
    _respond_to_invite("inv2", "active", user, db)
    refreshed = db.get(TenantWalkerAccess, "inv2")
    assert refreshed.status == "active"
    assert refreshed.requirements_met is True  # sem requisitos → ativo direto


# ── Task 4: endpoints (config / submit / approve) ───────────────────────────

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.core.database import get_db, get_walker_self_db  # noqa: E402
from app.dependencies.auth import get_current_user  # noqa: E402
from app.routes import walker_network as wn  # noqa: E402
from app.routes.walker_network import (  # noqa: E402
    ApproveRequirementsPayload,
    TenantRequirementsPayload,
    approve_walker_requirements,
    set_tenant_requirements,
)


def _admin_super():
    return User(id="adm1", email="adm@t.invalid", password_hash="x", role="super_admin",
                is_active=True, token_version=0, must_change_password=False)


def _walker_client(db, walker_id="w1"):
    app = FastAPI()
    app.include_router(wn.walker_router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_walker_self_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: db.get(User, walker_id)
    return TestClient(app)


def test_fluxo_completo_define_ve_submete_aprova():
    db = _db()
    _seed_tenant(db, "tA")
    _seed_walker(db, "w1")
    _link(db, "w1", "tA", requirements_met=True)
    admin = _admin_super()
    db.add(admin)
    db.commit()

    # 1) admin (super) define a lista de requisitos
    out = set_tenant_requirements("tA", TenantRequirementsPayload(requirements=["Curso de primeiros socorros"]), admin, db)
    assert out["requirements"] == ["Curso de primeiros socorros"]

    # 2) admin marca o par como pendente (re-verificação)
    approve_walker_requirements("w1", "tA", ApproveRequirementsPayload(requirements_met=False), admin, db)

    client = _walker_client(db)
    # 3) walker vê pendência
    g = client.get("/walker/network/tenants/tA/requirements")
    assert g.status_code == 200, g.text
    assert g.json()["requirements"] == ["Curso de primeiros socorros"]
    assert g.json()["status"] == "pending"

    # 4) walker submete → fila do admin
    s = client.post("/walker/network/tenants/tA/requirements/submit")
    assert s.status_code == 200 and s.json()["status"] == "submitted"
    assert client.get("/walker/network/tenants/tA/requirements").json()["status"] == "submitted"

    # 5) admin aprova → liberado
    approve_walker_requirements("w1", "tA", ApproveRequirementsPayload(requirements_met=True), admin, db)
    assert client.get("/walker/network/tenants/tA/requirements").json()["status"] == "met"


def test_admin_de_tenant_nao_edita_outro_tenant():
    db = _db()
    _seed_tenant(db, "tA")
    _seed_tenant(db, "tB")
    admin = User(id="adm2", email="a2@t.invalid", password_hash="x", role="admin",
                 is_active=True, token_version=0, must_change_password=False)
    admin.tenant_id = "tA"  # admin do tenant tA
    db.add(admin)
    db.commit()
    # tenta editar requisitos do tB → 404 (ensure_tenant_access bloqueia)
    try:
        set_tenant_requirements("tB", TenantRequirementsPayload(requirements=["x"]), admin, db)
        assert False, "deveria ter bloqueado o tenant errado"
    except HTTPException as e:
        assert e.status_code == 404
