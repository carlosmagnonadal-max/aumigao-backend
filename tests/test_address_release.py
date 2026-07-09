"""Liberação do endereço de coleta pro passeador (09/07).

BUG DE OPERAÇÃO: should_release_address exigia user.role == "walker", mas o app
só cria passeadores com role "passeador" → NENHUM passeador real recebia o
endereço do tutor. Segundo buraco: o set de status pulava pet_handover_confirmed
(endereço sumia com o walker na porta) e os status pós-passeio.

Regra: endereço só DEPOIS do aceite (pending_walker_confirmation continua
coarse — privacidade do tutor preservada no matching).
"""
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  — registra todas as tabelas no Base.metadata
from app.core.database import Base
from app.models.pet import Pet
from app.models.user import User
from app.models.walk import Walk
from app.services.operational_matching_service import (
    serialize_operational_walk,
    should_release_address,
)


def _db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _walker(role="passeador"):
    return User(id="w1", email="w@x.com", password_hash="x", role=role)


def _walk(op_status, walker_id="w1"):
    return Walk(
        id=str(uuid4()), tutor_id="t1", pet_id="p1", scheduled_date="2026-07-10T10:00",
        duration_minutes=45, price=50.0, status="Agendado", operational_status=op_status,
        walker_id=walker_id, address_snapshot="Rua das Flores, 123 — Pituba",
    )


def test_release_for_role_passeador_after_accept():
    """Role REAL do app é 'passeador' — endereço deve liberar após o aceite."""
    assert should_release_address(_walk("walker_accepted"), _walker("passeador")) is True


def test_release_for_role_walker_after_accept():
    assert should_release_address(_walk("walker_accepted"), _walker("walker")) is True


def test_release_during_pet_handover_confirmed():
    """Endereço não pode SUMIR com o walker na porta do tutor."""
    assert should_release_address(_walk("pet_handover_confirmed"), _walker("passeador")) is True


def test_release_during_awaiting_completion_review():
    assert should_release_address(_walk("awaiting_completion_review"), _walker("passeador")) is True


def test_no_release_before_accept():
    """Privacidade: no matching (pré-aceite) o endereço segue coarse."""
    assert should_release_address(_walk("pending_walker_confirmation"), _walker("passeador")) is False


def test_no_release_for_other_walker():
    walk = _walk("walker_accepted", walker_id="w2")
    assert should_release_address(walk, _walker("passeador")) is False


def test_serializer_gives_address_to_passeador_after_accept():
    db = _db()
    walker = _walker("passeador")
    db.add_all([
        walker,
        User(id="t1", email="t@x.com", password_hash="x", role="cliente"),
        Pet(id="p1", tutor_id="t1", name="Rex"),
    ])
    walk = _walk("walker_accepted")
    db.add(walk)
    db.commit()
    data = serialize_operational_walk(walk, db, user=walker)
    assert data["address_snapshot"] == "Rua das Flores, 123 — Pituba"
    assert data["pickup_privacy_level"] == "full"


# ---------------- snapshot vazio: fallback pro endereço do PERFIL do tutor ----------------
# BUG 09/07 (parte 2): o app agenda com address_snapshot="" fixo — o walk nascia
# SEM endereço mesmo com o perfil do tutor completo (cadastro step-2 coleta tudo).


def _tutor_profile(db):
    from app.models.tutor_profile import TutorProfile
    db.add(TutorProfile(
        id="tp1", user_id="t1", cep="41760150", street="Av. Paralela", number="3500",
        complement="ap 101", neighborhood="Trobogy", city="Salvador", state="BA",
        reference_point="Portaria azul",
    ))
    db.commit()


def test_serializer_falls_back_to_tutor_profile_address_when_snapshot_empty():
    db = _db()
    walker = _walker("passeador")
    db.add_all([
        walker,
        User(id="t1", email="t@x.com", password_hash="x", role="cliente"),
        Pet(id="p1", tutor_id="t1", name="Rex"),
    ])
    _tutor_profile(db)
    walk = _walk("walker_accepted")
    walk.address_snapshot = ""
    db.add(walk)
    db.commit()
    data = serialize_operational_walk(walk, db, user=walker)
    assert "Av. Paralela" in data["address_snapshot"]
    assert "3500" in data["address_snapshot"]
    assert "Trobogy" in data["address_snapshot"]


def test_serializer_no_profile_fallback_before_accept():
    """Pré-aceite continua coarse mesmo com snapshot vazio + perfil completo."""
    db = _db()
    walker = _walker("passeador")
    db.add_all([
        walker,
        User(id="t1", email="t@x.com", password_hash="x", role="cliente"),
        Pet(id="p1", tutor_id="t1", name="Rex"),
    ])
    _tutor_profile(db)
    walk = _walk("pending_walker_confirmation")
    walk.address_snapshot = ""
    db.add(walk)
    db.commit()
    data = serialize_operational_walk(walk, db, user=walker)
    assert data["address_snapshot"] == ""
