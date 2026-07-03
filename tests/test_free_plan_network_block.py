"""Plano free: REDE DESLIGADA — pool de matching de rede vazio para tenant free.

Fronteira do bloqueio: get_matching_pool_for_tenant (walker_network_matching_service),
o ponto único por onde matching_service e operational_matching_service resolvem os
passeadores de REDE do tenant. Pool vazio = tenant free só usa passeadores próprios.

Reverse trial ativo: plano efetivo "pro" → rede liberada durante o período de teste.
Zero-regressão: pro/enterprise mantêm o pool intacto.
"""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models import *  # noqa: F403
from app.models.tenant import Tenant
from app.models.tenant_walker_access import TenantWalkerAccess
from app.models.user import User
from app.models.walker_profile import WalkerProfile
from app.services.walker_network_matching_service import (
    get_matching_pool_for_tenant,
    tenant_network_blocked_by_plan,
)


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    session = Session()
    yield session
    session.close()


def _walker(db, walker_id: str) -> User:
    user = User(
        id=walker_id,
        email=f"{walker_id}@example.com",
        password_hash="x",
        full_name=walker_id,
        role="walker",
        is_active=True,
    )
    profile = WalkerProfile(
        id=f"profile-{walker_id}",
        user_id=user.id,
        full_name=walker_id,
        city="Salvador",
        state="BA",
        status="active",
        active_as_walker=True,
    )
    db.add_all([user, profile])
    return user


def _tenant_with_network_walker(db, tenant_id: str, plan: str, **tenant_kw) -> None:
    tenant = Tenant(id=tenant_id, name=tenant_id, slug=tenant_id, status="active", plan=plan, **tenant_kw)
    db.add(tenant)
    _walker(db, f"walker-{tenant_id}")
    db.add(
        TenantWalkerAccess(
            id=f"twa-{tenant_id}",
            tenant_id=tenant_id,
            walker_user_id=f"walker-{tenant_id}",
            access_type="shared_network",
            status="active",
        )
    )
    db.commit()


def test_free_tenant_network_pool_is_empty(db):
    _tenant_with_network_walker(db, "t-free", "free")
    assert tenant_network_blocked_by_plan(db, "t-free") is True
    assert get_matching_pool_for_tenant(db, "t-free") == []


def test_free_tenant_in_trial_has_network_pool(db):
    # Reverse trial ativo → plano efetivo pro → rede liberada.
    future = datetime.utcnow() + timedelta(days=10)
    _tenant_with_network_walker(db, "t-trial", "free", trial_ends_at=future)
    assert tenant_network_blocked_by_plan(db, "t-trial") is False
    assert get_matching_pool_for_tenant(db, "t-trial") == ["walker-t-trial"]


def test_free_tenant_expired_trial_pool_empty(db):
    past = datetime.utcnow() - timedelta(days=1)
    _tenant_with_network_walker(db, "t-exp", "free", trial_ends_at=past)
    assert get_matching_pool_for_tenant(db, "t-exp") == []


def test_pro_and_enterprise_pools_unchanged(db):
    _tenant_with_network_walker(db, "t-pro", "pro")
    _tenant_with_network_walker(db, "t-ent", "enterprise")
    assert tenant_network_blocked_by_plan(db, "t-pro") is False
    assert get_matching_pool_for_tenant(db, "t-pro") == ["walker-t-pro"]
    assert get_matching_pool_for_tenant(db, "t-ent") == ["walker-t-ent"]


def test_unknown_tenant_does_not_block(db):
    # Defensivo: tenant inexistente não bloqueia (comportamento legado do pool).
    assert tenant_network_blocked_by_plan(db, "nao-existe") is False
    assert get_matching_pool_for_tenant(db, "nao-existe") == []


# ── gate tenant-facing (tenant_tem_rede / enforce_network_access_allowed) ───

def test_tenant_tem_rede_free_is_false(db):
    from app.services.tenant_plan_service import tenant_tem_rede

    t = Tenant(id="t-f", name="t-f", slug="t-f", status="active", plan="free")
    db.add(t)
    db.commit()
    assert tenant_tem_rede(t, db) is False


def test_tenant_tem_rede_free_trial_is_true(db):
    from app.services.tenant_plan_service import tenant_tem_rede

    t = Tenant(
        id="t-ft", name="t-ft", slug="t-ft", status="active", plan="free",
        trial_ends_at=datetime.utcnow() + timedelta(days=5),
    )
    db.add(t)
    db.commit()
    assert tenant_tem_rede(t, db) is True


def test_tenant_tem_rede_pro_enterprise_unchanged(db):
    from app.services.tenant_plan_service import tenant_tem_rede

    pro = Tenant(id="t-p", name="t-p", slug="t-p", status="active", plan="pro")
    ent = Tenant(id="t-e", name="t-e", slug="t-e", status="active", plan="enterprise")
    db.add_all([pro, ent])
    db.commit()
    assert tenant_tem_rede(pro, db) is True
    assert tenant_tem_rede(ent, db) is True


def test_enforce_network_access_free_message(db):
    from fastapi import HTTPException
    from app.services.tenant_plan_service import enforce_network_access_allowed

    t = Tenant(id="t-fm", name="t-fm", slug="t-fm", status="active", plan="free")
    db.add(t)
    db.commit()
    try:
        enforce_network_access_allowed(t, db)
        raise AssertionError("deveria ter levantado 403")
    except HTTPException as exc:
        assert exc.status_code == 403
        assert "plano Pro" in exc.detail


def test_network_override_still_wins_for_free(db):
    # Override manual do super_admin tem precedência sobre a regra de plano
    # (hierarquia existente preservada — decisão explícita de plataforma).
    from app.services.tenant_plan_service import tenant_tem_rede

    t = Tenant(
        id="t-fo", name="t-fo", slug="t-fo", status="active", plan="free",
        network_access_override=True,
    )
    db.add(t)
    db.commit()
    assert tenant_tem_rede(t, db) is True
