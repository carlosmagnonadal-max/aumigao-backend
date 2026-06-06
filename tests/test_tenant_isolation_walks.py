from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models import *  # noqa: F403
from app.models.pet import Pet
from app.models.tenant import Tenant
from app.models.user import User
from app.models.walk import Walk
from app.routes.walks import _get_walk_for_user, list_walks


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    session = Session()
    yield session
    session.close()


@pytest.fixture()
def tenant_seed(db):
    tenant_a = Tenant(id="tenant-a", name="Tenant A", slug="tenant-a", status="active")
    tenant_b = Tenant(id="tenant-b", name="Tenant B", slug="tenant-b", status="active")

    admin_a = User(
        id="admin-a",
        email="admin-a@example.com",
        password_hash="x",
        full_name="Admin A",
        role="admin",
        tenant_id=tenant_a.id,
    )
    admin_b = User(
        id="admin-b",
        email="admin-b@example.com",
        password_hash="x",
        full_name="Admin B",
        role="admin",
        tenant_id=tenant_b.id,
    )
    super_admin = User(
        id="super-admin",
        email="super-admin@example.com",
        password_hash="x",
        full_name="Super Admin",
        role="super_admin",
        tenant_id=None,
    )
    tutor_a = User(
        id="tutor-a",
        email="tutor-a@example.com",
        password_hash="x",
        full_name="Tutor A",
        role="tutor",
        tenant_id=tenant_a.id,
    )
    tutor_b = User(
        id="tutor-b",
        email="tutor-b@example.com",
        password_hash="x",
        full_name="Tutor B",
        role="tutor",
        tenant_id=tenant_b.id,
    )
    pet_a = Pet(id="pet-a", tutor_id=tutor_a.id, tenant_id=tenant_a.id, name="Pet A", breed="SRD")
    pet_b = Pet(id="pet-b", tutor_id=tutor_b.id, tenant_id=tenant_b.id, name="Pet B", breed="SRD")

    scheduled_date = (datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S")
    walk_a = Walk(
        id="walk-a",
        tutor_id=tutor_a.id,
        tenant_id=tenant_a.id,
        pet_id=pet_a.id,
        scheduled_date=scheduled_date,
        duration_minutes=45,
        price=49.9,
        status="Agendado",
    )
    walk_b = Walk(
        id="walk-b",
        tutor_id=tutor_b.id,
        tenant_id=tenant_b.id,
        pet_id=pet_b.id,
        scheduled_date=scheduled_date,
        duration_minutes=45,
        price=49.9,
        status="Agendado",
    )

    db.add_all([
        tenant_a,
        tenant_b,
        admin_a,
        admin_b,
        super_admin,
        tutor_a,
        tutor_b,
        pet_a,
        pet_b,
        walk_a,
        walk_b,
    ])
    db.commit()

    return {
        "admin_a": admin_a,
        "admin_b": admin_b,
        "super_admin": super_admin,
        "walk_a": walk_a,
        "walk_b": walk_b,
    }


def listed_walk_ids(db, user: User) -> set[str]:
    return {item["id"] for item in list_walks(user=user, db=db, limit=50, full=False)}


def listed_full_walk_ids(db, user: User) -> set[str]:
    return {item["id"] for item in list_walks(user=user, db=db, limit=50, full=True)}


def test_admin_a_does_not_list_walk_b(db, tenant_seed):
    assert listed_walk_ids(db, tenant_seed["admin_a"]) == {"walk-a"}


def test_admin_b_does_not_list_walk_a(db, tenant_seed):
    assert listed_walk_ids(db, tenant_seed["admin_b"]) == {"walk-b"}


def test_super_admin_lists_both_tenant_walks(db, tenant_seed):
    assert listed_walk_ids(db, tenant_seed["super_admin"]) == {"walk-a", "walk-b"}


def test_admin_a_full_listing_does_not_list_walk_b(db, tenant_seed):
    assert listed_full_walk_ids(db, tenant_seed["admin_a"]) == {"walk-a"}


def test_admin_b_full_listing_does_not_list_walk_a(db, tenant_seed):
    assert listed_full_walk_ids(db, tenant_seed["admin_b"]) == {"walk-b"}


def test_super_admin_full_listing_lists_both_tenant_walks(db, tenant_seed):
    assert listed_full_walk_ids(db, tenant_seed["super_admin"]) == {"walk-a", "walk-b"}


def test_admin_a_cannot_access_walk_b_by_id(db, tenant_seed):
    with pytest.raises(HTTPException) as exc_info:
        _get_walk_for_user("walk-b", tenant_seed["admin_a"], db)

    assert exc_info.value.status_code == 404


def test_admin_b_cannot_access_walk_a_by_id(db, tenant_seed):
    with pytest.raises(HTTPException) as exc_info:
        _get_walk_for_user("walk-a", tenant_seed["admin_b"], db)

    assert exc_info.value.status_code == 404


def test_super_admin_accesses_both_tenant_walks_by_id(db, tenant_seed):
    assert _get_walk_for_user("walk-a", tenant_seed["super_admin"], db).id == "walk-a"
    assert _get_walk_for_user("walk-b", tenant_seed["super_admin"], db).id == "walk-b"
