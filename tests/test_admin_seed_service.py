"""Testes do seed de admin (app/services/admin_seed_service.py).

SEC: o seed NÃO pode sobrescrever uma conta admin já existente (password_hash/
is_active/role). Reescrever a senha do env a cada boot reverteria uma rotação de
senha feita pela UI. Só cria quando a conta não existe.

Padrão do projeto: SQLite em memória (StaticPool), sem tocar produção.
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 — registra as tabelas
from app.core.database import Base
from app.core.security import get_password_hash, verify_password
from app.models.tenant import Tenant
from app.models.user import User
from app.services.admin_seed_service import ensure_configured_admin_users
from app.services.tenant_seed_service import DEFAULT_TENANT_SLUG

TENANT_ID = "t-seed"


def _session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(Tenant(id=TENANT_ID, name="Aumigao", slug=DEFAULT_TENANT_SLUG, status="active", plan="business"))
    db.commit()
    return db


@pytest.fixture(autouse=True)
def _admin_env(monkeypatch):
    monkeypatch.setenv("ADMIN_EMAIL", "admin@aumigao.com")
    monkeypatch.setenv("ADMIN_PASSWORD", "SenhaEnv@123")
    # Garante que o super_admin não interfira nos testes de admin.
    monkeypatch.delenv("SUPER_ADMIN_EMAIL", raising=False)
    monkeypatch.delenv("SUPER_ADMIN_PASSWORD", raising=False)


def test_seed_creates_admin_when_absent():
    db = _session()
    ensure_configured_admin_users(db)
    user = db.query(User).filter(User.email == "admin@aumigao.com").one()
    assert user.role == "admin"
    assert user.is_active is True
    assert verify_password("SenhaEnv@123", user.password_hash)


def test_seed_does_not_overwrite_existing_account():
    """Conta já existe com senha ROTACIONADA pela UI e is_active=False, role diferente.

    O seed NÃO pode reanimar a senha do env nem reativar/re-elevar a conta.
    """
    db = _session()
    rotated_hash = get_password_hash("SenhaNovaDaUI@999")
    db.add(User(
        id="u-admin-existente",
        email="admin@aumigao.com",
        full_name="Admin Existente",
        role="tutor",             # role diferente do env ("admin")
        password_hash=rotated_hash,
        tenant_id=TENANT_ID,
        is_active=False,          # desativado de propósito
    ))
    db.commit()

    ensure_configured_admin_users(db)

    db.expire_all()
    user = db.query(User).filter(User.email == "admin@aumigao.com").one()
    # Nada sobrescrito:
    assert user.password_hash == rotated_hash
    assert verify_password("SenhaNovaDaUI@999", user.password_hash)
    assert not verify_password("SenhaEnv@123", user.password_hash)
    assert user.is_active is False
    assert user.role == "tutor"
