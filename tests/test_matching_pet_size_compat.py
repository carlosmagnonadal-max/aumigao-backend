"""Tarefa A (Wave 5) — compatibilidade de porte pet ↔ passeador (TDD).

Regra: passeador só é elegível se max_dog_size >= porte do pet.
- default "Grande" => aceita todos (ZERO regressão).
- FAIL-OPEN: porte do pet desconhecido/"" OU max_dog_size desconhecido => NÃO filtra.

Infra: SQLite em memória com as tabelas usadas pelo service, incluindo Pet
(necessária agora porque o filtro carrega o pet via pet_id).
"""
from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.pet import Pet
from app.models.walk import Walk
from app.models.walker_profile import WalkerProfile
from app.models.walker_review import WalkerReview
from app.schemas.matching import MatchingWalkerRequest
from app.services import matching_service as svc


def _db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[
            WalkerProfile.__table__,
            Walk.__table__,
            WalkerReview.__table__,
            Pet.__table__,
        ],
    )
    return sessionmaker(bind=engine)()


_seq = {"n": 0}


def _profile(db, *, user_id, max_dog_size=None, city="salvador", state="pituba"):
    _seq["n"] += 1
    n = _seq["n"]
    kwargs = dict(
        id=f"wp-{n}",
        user_id=user_id,
        full_name=f"P{n}",
        city=city,
        state=state,
        status="active",
        active_as_walker=True,
        created_at=datetime(2024, 1, 1, 12, 0, 0),
    )
    if max_dog_size is not None:
        kwargs["max_dog_size"] = max_dog_size
    profile = WalkerProfile(**kwargs)
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


def _pet(db, *, pet_id, size):
    pet = Pet(id=pet_id, tutor_id="tutor-1", name="Rex", size=size)
    db.add(pet)
    db.commit()
    return pet


def _request(**kwargs):
    base = {"city": "salvador", "neighborhood": "pituba"}
    base.update(kwargs)
    return MatchingWalkerRequest(**base)


# --------------------------------------------------------------------------- #
# Helper de ordem de porte
# --------------------------------------------------------------------------- #
def test_size_rank_normalizes_accent_and_case():
    assert svc.size_rank("Pequeno") == 1
    assert svc.size_rank("MÉDIO") == 2
    assert svc.size_rank("medio") == 2
    assert svc.size_rank("Grande") == 3
    assert svc.size_rank("grande") == 3


def test_size_rank_unknown_returns_none():
    assert svc.size_rank("") is None
    assert svc.size_rank(None) is None
    assert svc.size_rank("Gigante") is None


# --------------------------------------------------------------------------- #
# Filtro em get_eligible_walkers
# --------------------------------------------------------------------------- #
def test_small_only_walker_excluded_for_large_pet():
    db = _db()
    _pet(db, pet_id="pet-grande", size="Grande")
    _profile(db, user_id="so-pequeno", max_dog_size="Pequeno")
    _profile(db, user_id="grande", max_dog_size="Grande")
    result = svc.get_eligible_walkers(_request(pet_id="pet-grande"), db)
    assert [p.user_id for p in result] == ["grande"]


def test_medium_walker_accepts_small_and_medium_not_large():
    db = _db()
    _pet(db, pet_id="pet-medio", size="Médio")
    _pet(db, pet_id="pet-grande", size="Grande")
    _profile(db, user_id="medio", max_dog_size="Médio")
    # pet médio -> elegível
    res_medio = svc.get_eligible_walkers(_request(pet_id="pet-medio"), db)
    assert [p.user_id for p in res_medio] == ["medio"]
    # pet grande -> excluído
    res_grande = svc.get_eligible_walkers(_request(pet_id="pet-grande"), db)
    assert res_grande == []


def test_default_grande_is_eligible_for_any_size_zero_regression():
    db = _db()
    _pet(db, pet_id="pet-grande", size="Grande")
    # max_dog_size não passado -> server_default "Grande" no banco
    _profile(db, user_id="default", max_dog_size=None)
    result = svc.get_eligible_walkers(_request(pet_id="pet-grande"), db)
    assert [p.user_id for p in result] == ["default"]


def test_failopen_unknown_pet_size_does_not_filter():
    db = _db()
    _pet(db, pet_id="pet-gigante", size="Gigante")  # valor desconhecido
    _pet(db, pet_id="pet-vazio", size="")           # vazio
    _profile(db, user_id="so-pequeno", max_dog_size="Pequeno")
    # porte desconhecido => não filtra (mantém recall)
    res1 = svc.get_eligible_walkers(_request(pet_id="pet-gigante"), db)
    assert [p.user_id for p in res1] == ["so-pequeno"]
    res2 = svc.get_eligible_walkers(_request(pet_id="pet-vazio"), db)
    assert [p.user_id for p in res2] == ["so-pequeno"]


def test_failopen_unknown_max_dog_size_does_not_filter():
    db = _db()
    _pet(db, pet_id="pet-grande", size="Grande")
    _profile(db, user_id="desconhecido", max_dog_size="Gigante")  # max desconhecido
    result = svc.get_eligible_walkers(_request(pet_id="pet-grande"), db)
    assert [p.user_id for p in result] == ["desconhecido"]


def test_no_pet_id_does_not_filter_zero_regression():
    db = _db()
    _profile(db, user_id="so-pequeno", max_dog_size="Pequeno")
    # request sem pet_id (chamadas que não passam pet) => não filtra
    result = svc.get_eligible_walkers(_request(pet_id=None), db)
    assert [p.user_id for p in result] == ["so-pequeno"]


def test_missing_pet_row_does_not_filter():
    db = _db()
    _profile(db, user_id="so-pequeno", max_dog_size="Pequeno")
    # pet_id aponta para pet inexistente => fail-open
    result = svc.get_eligible_walkers(_request(pet_id="nao-existe"), db)
    assert [p.user_id for p in result] == ["so-pequeno"]
