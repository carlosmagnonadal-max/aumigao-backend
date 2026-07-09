"""Código de Coleta (09/07): prova de entrega presencial do pet.

Backend gera 4 dígitos por walk; só tutor/admin veem; walker valida no
pet-handover (flag pickup_code_required, default ON; NULL = grandfather).
Plano: docs/superpowers/plans/2026-07-09-codigo-de-coleta.md
"""
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  — registra todas as tabelas no Base.metadata
from app.core.database import Base
from app.models.walk import Walk


def _db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _walk(**kw):
    return Walk(
        id=str(uuid4()), tutor_id="t1", pet_id="p1", scheduled_date="2026-07-10T10:00",
        duration_minutes=45, price=50.0, **kw,
    )


# ---------------- Task 1: geração ----------------
def test_new_walk_gets_4_digit_security_code():
    db = _db()
    walk = _walk()
    db.add(walk)
    db.commit()
    db.refresh(walk)
    assert walk.security_code is not None
    assert len(walk.security_code) == 4
    assert walk.security_code.isdigit()


def test_security_codes_vary_between_walks():
    db = _db()
    codes = set()
    for _ in range(20):
        w = _walk()
        db.add(w)
        db.commit()
        db.refresh(w)
        codes.add(w.security_code)
    assert len(codes) > 1  # gerador aleatório, não constante
