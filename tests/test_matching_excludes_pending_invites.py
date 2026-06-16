"""net-T3 — convites pending/declined NÃO entram no pool de matching do tenant.

Só TenantWalkerAccess.status == "active" torna o passeador elegível para o tenant.
Trava de regressão sobre walker_network_matching_service (foundation do matching).
"""
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
    get_tenant_eligible_walker_ids,
    is_walker_eligible_for_tenant,
)

TENANT_ID = "tenant-net3"


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autocommit=False, autoflush=False)()
    yield session
    session.close()


def _walker(db, wid):
    db.add(User(id=wid, email=f"{wid}@x.com", password_hash="x", full_name="P", role="walker", is_active=True))
    db.add(WalkerProfile(id=f"p-{wid}", user_id=wid, full_name="P", status="active", active_as_walker=True))


def _access(db, wid, status):
    db.add(
        TenantWalkerAccess(
            id=f"a-{wid}",
            tenant_id=TENANT_ID,
            walker_user_id=wid,
            access_type="shared_network",
            status=status,
        )
    )


@pytest.fixture()
def seed(db):
    db.add(Tenant(id=TENANT_ID, name="T", slug="tenant-net3", status="active"))
    for wid, status in (
        ("w-active", "active"),
        ("w-pending", "pending"),
        ("w-declined", "declined"),
    ):
        _walker(db, wid)
        _access(db, wid, status)
    db.commit()


def test_pending_walker_not_in_pool(db, seed):
    pool = get_tenant_eligible_walker_ids(db, TENANT_ID)
    assert "w-pending" not in pool
    assert is_walker_eligible_for_tenant(db, TENANT_ID, "w-pending") is False


def test_declined_walker_not_in_pool(db, seed):
    pool = get_tenant_eligible_walker_ids(db, TENANT_ID)
    assert "w-declined" not in pool
    assert is_walker_eligible_for_tenant(db, TENANT_ID, "w-declined") is False


def test_only_active_walker_in_pool(db, seed):
    pool = get_tenant_eligible_walker_ids(db, TENANT_ID)
    assert pool == ["w-active"]
    assert is_walker_eligible_for_tenant(db, TENANT_ID, "w-active") is True


def test_accepting_pending_makes_walker_eligible(db, seed):
    # transição pending -> active (como o endpoint /accept faz) entra no pool
    access = db.query(TenantWalkerAccess).filter_by(id="a-w-pending").one()
    access.status = "active"
    db.commit()
    assert "w-pending" in get_tenant_eligible_walker_ids(db, TENANT_ID)
    assert is_walker_eligible_for_tenant(db, TENANT_ID, "w-pending") is True
