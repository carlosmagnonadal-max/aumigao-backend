"""T15 — Testes do model PetShareLink + migration 0076."""
from __future__ import annotations

import app.models  # noqa: F401 — garante registro de todos os models

from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.models.pet import Pet
from app.models.pet_share_link import PetShareLink
from app.models.tenant import Tenant
from app.models.user import User


def _db():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    session = sessionmaker(bind=eng)()
    session.add(Tenant(id="t1", name="T1", slug="t1", status="active", plan="business"))
    session.add(User(id="u1", email="u@x.com", password_hash="x", role="tutor", tenant_id="t1"))
    session.add(Pet(id="p1", tutor_id="u1", tenant_id="t1", name="Rex"))
    session.commit()
    return session


def test_create_pet_share_link():
    """PetShareLink pode ser inserido e recuperado com todos os campos."""
    db = _db()
    now = datetime.utcnow()
    link = PetShareLink(
        id="lnk1",
        token="tok_abc123",
        pet_id="p1",
        tenant_id="t1",
        created_by="u1",
        consent_at=now,
        expires_at=now + timedelta(days=30),
        revoked_at=None,
        created_at=now,
    )
    db.add(link)
    db.commit()

    fetched = db.get(PetShareLink, "lnk1")
    assert fetched is not None
    assert fetched.token == "tok_abc123"
    assert fetched.pet_id == "p1"
    assert fetched.tenant_id == "t1"
    assert fetched.created_by == "u1"
    assert fetched.consent_at is not None
    assert fetched.expires_at > fetched.created_at
    assert fetched.revoked_at is None


def test_token_is_unique():
    """token tem constraint UNIQUE — segundo insert com mesmo token deve falhar."""
    from sqlalchemy.exc import IntegrityError

    db = _db()
    now = datetime.utcnow()

    db.add(PetShareLink(
        id="lnk1", token="same_token", pet_id="p1", tenant_id="t1",
        created_by="u1", consent_at=now, expires_at=now + timedelta(days=30), created_at=now,
    ))
    db.commit()

    db.add(PetShareLink(
        id="lnk2", token="same_token", pet_id="p1", tenant_id="t1",
        created_by="u1", consent_at=now, expires_at=now + timedelta(days=30), created_at=now,
    ))
    try:
        db.commit()
        assert False, "Deveria ter lançado IntegrityError"
    except IntegrityError:
        db.rollback()


def test_revoke_link():
    """revoked_at pode ser setado para revogar o link."""
    db = _db()
    now = datetime.utcnow()
    link = PetShareLink(
        id="lnk1", token="tok_x", pet_id="p1", tenant_id="t1",
        created_by="u1", consent_at=now, expires_at=now + timedelta(days=30), created_at=now,
    )
    db.add(link)
    db.commit()

    link.revoked_at = now
    db.commit()

    fetched = db.get(PetShareLink, "lnk1")
    assert fetched.revoked_at is not None


def test_tenant_id_nullable():
    """tenant_id pode ser NULL (link criado sem tenant específico)."""
    db = _db()
    now = datetime.utcnow()
    link = PetShareLink(
        id="lnk1", token="tok_notenant", pet_id="p1", tenant_id=None,
        created_by="u1", consent_at=now, expires_at=now + timedelta(days=30), created_at=now,
    )
    db.add(link)
    db.commit()

    fetched = db.get(PetShareLink, "lnk1")
    assert fetched.tenant_id is None
