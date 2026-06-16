"""C11 / mt-MT3 — a PREVIEW de matching não vaza passeadores cross-tenant.

Antes: a rota pública de matching rankeava o pool GLOBAL (qualquer walker ativo).
Agora get_eligible_walkers, quando recebe tenant_id, restringe ao pool da REDE do
tenant (mesmo critério da alocação vinculante). Sem tenant_id mantém o comportamento
legado (compat).
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.core.database import Base
from app.models.tenant import Tenant
from app.models.tenant_walker_access import TenantWalkerAccess
from app.models.user import User
from app.models.walker_profile import WalkerProfile
from app.schemas.matching import MatchingWalkerRequest
from app.services import matching_service as svc


def _db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _walker(db, uid, tenant_id, *, city="salvador"):
    db.add(User(id=uid, email=f"{uid}@x.com", password_hash="x", role="walker", is_active=True, tenant_id=tenant_id))
    db.add(WalkerProfile(id=f"wp-{uid}", user_id=uid, full_name=uid, status="active", active_as_walker=True, city=city, state="ba"))


def _link(db, uid, tenant_id, status="active", access_type="shared_network"):
    db.add(TenantWalkerAccess(id=f"twa-{uid}-{tenant_id}", tenant_id=tenant_id, walker_user_id=uid,
                              status=status, access_type=access_type))


def _setup():
    db = _db()
    db.add(Tenant(id="t1", name="T1", slug="t1", status="active", plan="business"))
    db.add(Tenant(id="t2", name="T2", slug="t2", status="active", plan="business"))
    _walker(db, "wa", "t1")
    _walker(db, "wb", "t2")
    _link(db, "wa", "t1")          # wa na rede do t1
    _link(db, "wb", "t2")          # wb na rede do t2
    db.commit()
    return db


def _req():
    return MatchingWalkerRequest(city="salvador", neighborhood="pituba")


def test_eligible_walkers_without_tenant_returns_all_active():
    db = _setup()
    ids = {p.user_id for p in svc.get_eligible_walkers(_req(), db)}
    assert {"wa", "wb"}.issubset(ids)  # comportamento legado preservado


def test_eligible_walkers_with_tenant_excludes_other_tenant():
    db = _setup()
    ids = {p.user_id for p in svc.get_eligible_walkers(_req(), db, tenant_id="t1")}
    assert "wa" in ids        # da rede do t1
    assert "wb" not in ids    # NÃO vaza o walker exclusivo do t2


def test_eligible_walkers_tenant_without_network_returns_empty():
    db = _setup()
    # t-vazio não tem nenhum TenantWalkerAccess ativo -> pool vazio -> nenhum walker
    ids = {p.user_id for p in svc.get_eligible_walkers(_req(), db, tenant_id="t-vazio")}
    assert ids == set()


def test_pending_invite_not_in_preview_pool():
    db = _db()
    db.add(Tenant(id="t1", name="T1", slug="t1", status="active", plan="business"))
    _walker(db, "wp", "t1")
    _link(db, "wp", "t1", status="pending")  # convidado, ainda não aceitou
    db.commit()
    ids = {p.user_id for p in svc.get_eligible_walkers(_req(), db, tenant_id="t1")}
    assert "wp" not in ids
