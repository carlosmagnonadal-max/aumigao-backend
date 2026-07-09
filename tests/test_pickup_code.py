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


# ---------------- Task 2: serialização com gate de papel ----------------
from app.models.user import User  # noqa: E402
from app.services.operational_matching_service import serialize_operational_walk  # noqa: E402


def _users(db):
    tutor = User(id="t1", email="t@x.com", password_hash="x", role="cliente")
    walker = User(id="w1", email="w@x.com", password_hash="x", role="passeador")
    admin = User(id="a1", email="a@x.com", password_hash="x", role="super_admin")
    db.add_all([tutor, walker, admin])
    db.commit()
    return tutor, walker, admin


def test_serializer_shows_code_to_tutor_and_admin_always():
    """Tutor (dono) e admin veem o código em qualquer status — o tutor precisa
    dele pra CONFERIR o que o passeador informar (inversão 09/07)."""
    db = _db()
    tutor, walker, admin = _users(db)
    from app.models.pet import Pet
    db.add(Pet(id="p1", tutor_id="t1", name="Rex"))
    walk = _walk(walker_id="w1", operational_status="pending_walker_confirmation")
    db.add(walk)
    db.commit()

    as_tutor = serialize_operational_walk(walk, db, user=tutor)
    as_walker = serialize_operational_walk(walk, db, user=walker)
    as_admin = serialize_operational_walk(walk, db, user=admin)

    assert as_tutor["security_code"] == walk.security_code
    assert as_admin["security_code"] == walk.security_code
    # Pré-aceite o walker ainda NÃO vê (só depois do aceite — ver testes abaixo).
    assert as_walker["security_code"] is None


# ---------------- Task 3: gate no pet-handover ----------------
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.core.database import get_db  # noqa: E402
from app.dependencies.auth import get_current_user  # noqa: E402
from app.models.tenant import Tenant, TenantFeature  # noqa: E402
from app.models.walker_profile import WalkerProfile  # noqa: E402
from app.routes import walker as walker_module  # noqa: E402


def _handover_app(db, walker_user_id="w1"):
    test_app = FastAPI()
    test_app.include_router(walker_module.router)
    test_app.dependency_overrides[get_db] = lambda: db
    test_app.dependency_overrides[get_current_user] = lambda: db.get(User, walker_user_id)
    return TestClient(test_app)


def _seed_handover(db, *, code="1234", feature_on=True):
    db.add(Tenant(id="tn1", name="T", slug="aumigao", status="active", plan="business"))
    if not feature_on:
        db.add(TenantFeature(tenant_id="tn1", feature_key="pickup_code_required", enabled=False))
    _users(db)
    from app.models.pet import Pet
    db.add(Pet(id="p1", tutor_id="t1", tenant_id="tn1", name="Rex"))
    db.add(WalkerProfile(id="wp1", user_id="w1", status="active", active_as_walker=True))
    walk = _walk(walker_id="w1", tenant_id="tn1", operational_status="walker_arriving", status="Indo buscar o pet")
    db.add(walk)
    db.commit()
    walk.security_code = code
    db.commit()
    db.refresh(walk)
    return walk


# INVERSÃO (decisão Carlos 09/07 tarde): o código protege o TUTOR — é o
# PASSEADOR quem informa o código ao tutor, que confere no app dele antes de
# entregar o pet. O pet-handover NÃO exige mais digitação (o app walker antigo
# pode mandar security_code — o backend aceita e ignora).


def test_handover_passes_without_code():
    db = _db()
    walk = _seed_handover(db, code="1234")
    client = _handover_app(db)
    r = client.post(f"/walker/walks/{walk.id}/pet-handover", json={})
    assert r.status_code == 200, r.text
    db.refresh(walk)
    assert walk.operational_status == "pet_handover_confirmed"


def test_handover_ignores_legacy_code_payload():
    """OTA walker antiga ainda envia security_code — não pode travar nem errado."""
    db = _db()
    walk = _seed_handover(db, code="1234")
    client = _handover_app(db)
    r = client.post(f"/walker/walks/{walk.id}/pet-handover", json={"security_code": "9999"})
    assert r.status_code == 200, r.text


def test_assigned_walker_sees_code_after_accept():
    """Inversão: o walker atribuído RECEBE o código (pra informar ao tutor)."""
    db = _db()
    walk = _seed_handover(db, code="1234")  # walker_arriving, atribuído a w1
    walker = db.get(User, "w1")
    data = serialize_operational_walk(walk, db, user=walker)
    assert data["security_code"] == "1234"


def test_walker_does_not_see_code_before_accept():
    """Pré-aceite o código NÃO vai — evita vazar antes do matching fechar."""
    db = _db()
    walk = _seed_handover(db, code="1234")
    walk.operational_status = "pending_walker_confirmation"
    db.commit()
    walker = db.get(User, "w1")
    data = serialize_operational_walk(walk, db, user=walker)
    assert data["security_code"] is None
