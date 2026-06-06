import pytest
from fastapi import HTTPException
from sqlalchemy import column

from app.dependencies.tenant_scope import (
    AdminTenantScope,
    apply_tenant_filter,
    ensure_tenant_access,
    get_admin_tenant_scope,
)
from app.models.user import User


class FakeQuery:
    def __init__(self):
        self.filters = []

    def filter(self, *criteria):
        self.filters.extend(criteria)
        return self


class ModelWithTenant:
    tenant_id = column("tenant_id")


class ModelWithoutTenant:
    pass


def make_user(role: str, tenant_id: str | None = None) -> User:
    return User(
        id=f"{role}-user",
        email=f"{role}@example.com",
        password_hash="hash",
        role=role,
        tenant_id=tenant_id,
    )


def test_admin_without_tenant_id_raises_http_exception():
    admin = make_user("admin", tenant_id=None)

    with pytest.raises(HTTPException) as exc_info:
        get_admin_tenant_scope(admin)

    assert exc_info.value.status_code in {400, 422}


def test_admin_with_tenant_id_generates_non_global_scope():
    admin = make_user("admin", tenant_id="tenant-a")

    scope = get_admin_tenant_scope(admin)

    assert scope.user is admin
    assert scope.tenant_id == "tenant-a"
    assert scope.is_global is False
    assert scope.role == "admin"


def test_super_admin_generates_global_scope():
    super_admin = make_user("super_admin", tenant_id="tenant-a")

    scope = get_admin_tenant_scope(super_admin)

    assert scope.user is super_admin
    assert scope.tenant_id is None
    assert scope.is_global is True
    assert scope.role == "super_admin"


def test_apply_tenant_filter_with_global_scope_returns_query_unchanged():
    query = FakeQuery()
    scope = AdminTenantScope(
        user=make_user("super_admin"),
        tenant_id=None,
        is_global=True,
        role="super_admin",
    )

    result = apply_tenant_filter(query, ModelWithTenant, scope)

    assert result is query
    assert query.filters == []


def test_ensure_tenant_access_blocks_different_tenant_with_404():
    scope = AdminTenantScope(
        user=make_user("admin", tenant_id="tenant-a"),
        tenant_id="tenant-a",
        is_global=False,
        role="admin",
    )

    with pytest.raises(HTTPException) as exc_info:
        ensure_tenant_access("tenant-b", scope)

    assert exc_info.value.status_code == 404


def test_ensure_tenant_access_allows_same_tenant():
    scope = AdminTenantScope(
        user=make_user("admin", tenant_id="tenant-a"),
        tenant_id="tenant-a",
        is_global=False,
        role="admin",
    )

    assert ensure_tenant_access("tenant-a", scope) is None


def test_apply_tenant_filter_requires_tenant_column_for_model_without_tenant_id():
    query = FakeQuery()
    scope = AdminTenantScope(
        user=make_user("admin", tenant_id="tenant-a"),
        tenant_id="tenant-a",
        is_global=False,
        role="admin",
    )

    with pytest.raises(ValueError):
        apply_tenant_filter(query, ModelWithoutTenant, scope)
