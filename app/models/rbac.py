"""Modelos de RBAC/ABAC (Sprint 15).

Estrutura de papéis e permissões por tenant/unidade. Convive com o `role` string
atual em users durante a migração — ver app/dependencies/auth.py.
"""
from datetime import datetime
from uuid import uuid4

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Role(Base):
    __tablename__ = "roles"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid4()))
    # global_admin, tenant_admin, tenant_operator, unit_operator, tutor, walker
    name: Mapped[str] = mapped_column(String, unique=True, index=True)
    description: Mapped[str | None] = mapped_column(String, nullable=True)
    scope_type: Mapped[str] = mapped_column(String, default="global")  # global | tenant | unit
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Permission(Base):
    __tablename__ = "permissions"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid4()))
    key: Mapped[str] = mapped_column(String, unique=True, index=True)  # ex: walks.update_status
    description: Mapped[str | None] = mapped_column(String, nullable=True)
    module: Mapped[str] = mapped_column(String, index=True)  # ex: walks
    action: Mapped[str] = mapped_column(String)  # ex: update_status
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class RolePermission(Base):
    __tablename__ = "role_permissions"
    __table_args__ = (UniqueConstraint("role_id", "permission_id", name="uq_role_permissions"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid4()))
    role_id: Mapped[str] = mapped_column(String, ForeignKey("roles.id"), index=True)
    permission_id: Mapped[str] = mapped_column(String, ForeignKey("permissions.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class UserRoleAssignment(Base):
    __tablename__ = "user_role_assignments"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid4()))
    user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), index=True)
    role_id: Mapped[str] = mapped_column(String, ForeignKey("roles.id"), index=True)
    # Escopo do papel: nulo = global; preenchido = restrito ao tenant/unidade.
    tenant_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    tenant_unit_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    created_by: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
