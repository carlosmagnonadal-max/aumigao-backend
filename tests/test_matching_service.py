"""Testes de unidade do matching_service.

Foco: get_eligible_walkers (gating active/active_as_walker, has_vehicle para
pet_tour, exclusao de risk suspended), os scores puros (proximity/rating/
experience) e has_schedule_conflict.

Padrao dos testes do repo: SQLite em memoria, criando SO as tabelas usadas pelo
service (sem app.main, sem alembic, sem banco real). As tabelas necessarias sao
WalkerProfile + Walk + WalkerReview (esta ultima usada por
calculate_hybrid_reputation_score/reputation_summary).
"""
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.walk import Walk
from app.models.walker_availability_exception import WalkerAvailabilityException
from app.models.walker_profile import WalkerProfile
from app.models.walker_review import WalkerReview
from app.schemas.matching import MatchingWalkerRequest
from app.services import matching_service as svc


# --------------------------------------------------------------------------- #
# Infra
# --------------------------------------------------------------------------- #
def _db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[
            WalkerProfile.__table__,
            Walk.__table__,
            WalkerReview.__table__,
            WalkerAvailabilityException.__table__,
        ],
    )
    return sessionmaker(bind=engine)()


_seq = {"n": 0}


def _profile(
    db,
    *,
    user_id=None,
    status="active",
    active_as_walker=True,
    has_vehicle=False,
    city="salvador",
    state="pituba",
    cpf="",
    created_at=None,
) -> WalkerProfile:
    _seq["n"] += 1
    n = _seq["n"]
    uid = user_id if user_id is not None else f"user-{n}"
    profile = WalkerProfile(
        id=f"wp-{n}",
        user_id=uid,
        full_name=f"Passeador {n}",
        cpf=cpf,
        city=city,
        state=state,
        status=status,
        active_as_walker=active_as_walker,
        has_vehicle=has_vehicle,
        created_at=created_at or datetime(2024, 1, 1, 12, 0, 0),
    )
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


def _request(**kwargs) -> MatchingWalkerRequest:
    base = {"city": "salvador", "neighborhood": "pituba"}
    base.update(kwargs)
    return MatchingWalkerRequest(**base)


def _walk(db, *, walker_id, scheduled_date, duration_minutes=45, status="Agendado") -> Walk:
    _seq["n"] += 1
    n = _seq["n"]
    walk = Walk(
        id=f"walk-{n}",
        tutor_id=f"tutor-{n}",
        walker_id=walker_id,
        pet_id=f"pet-{n}",
        scheduled_date=scheduled_date,
        duration_minutes=duration_minutes,
        price=50.0,
        status=status,
    )
    db.add(walk)
    db.commit()
    return walk


# --------------------------------------------------------------------------- #
# Helpers puros: parse_datetime / clamp / normalize
# --------------------------------------------------------------------------- #
def test_parse_datetime_formats_and_invalid():
    assert svc.parse_datetime("2024-05-10T14:30:00") == datetime(2024, 5, 10, 14, 30, 0)
    assert svc.parse_datetime("2024-05-10T14:30") == datetime(2024, 5, 10, 14, 30)
    assert svc.parse_datetime("2024-05-10") == datetime(2024, 5, 10)
    assert svc.parse_datetime(None) is None
    assert svc.parse_datetime("") is None
    assert svc.parse_datetime("nao-e-data") is None


def test_clamp_and_normalize():
    assert svc.clamp(150) == 100
    assert svc.clamp(-5) == 0
    assert svc.clamp(50) == 50
    assert svc.normalize("  Pituba ") == "pituba"
    assert svc.normalize(None) == ""


# --------------------------------------------------------------------------- #
# calculate_proximity_score
# --------------------------------------------------------------------------- #
def test_proximity_same_neighborhood_is_best():
    profile = WalkerProfile(id="x", user_id="u", city="salvador", state="pituba")
    score, dist = svc.calculate_proximity_score(profile, _request(city="salvador", neighborhood="pituba"))
    assert score == 90.0
    assert dist == 1.6


def test_proximity_nearby_neighborhood():
    # state do walker = "itaigara" esta em NEARBY_NEIGHBORHOODS["pituba"].
    profile = WalkerProfile(id="x", user_id="u", city="salvador", state="itaigara")
    score, dist = svc.calculate_proximity_score(profile, _request(city="salvador", neighborhood="pituba"))
    assert score == 70.0
    assert dist == 3.8


def test_proximity_same_city_different_neighborhood():
    profile = WalkerProfile(id="x", user_id="u", city="salvador", state="cabula")
    score, dist = svc.calculate_proximity_score(profile, _request(city="salvador", neighborhood="pituba"))
    assert score == 50.0
    assert dist == 7.5


def test_proximity_different_city_is_zero():
    profile = WalkerProfile(id="x", user_id="u", city="lauro de freitas", state="centro")
    score, dist = svc.calculate_proximity_score(profile, _request(city="salvador", neighborhood="pituba"))
    assert score == 0.0
    assert dist is None


def test_proximity_no_location_data_falls_back():
    # Sem cidade/bairro no request -> ramo default (65/5.0).
    profile = WalkerProfile(id="x", user_id="u", city="", state="")
    score, dist = svc.calculate_proximity_score(profile, _request(city=None, neighborhood=None))
    assert score == 65.0
    assert dist == 5.0


# --------------------------------------------------------------------------- #
# calculate_rating_score
# --------------------------------------------------------------------------- #
def test_rating_score_no_reviews_default():
    assert svc.calculate_rating_score({"reviews_count": 0, "rating_average": 0.0}) == 75.0


def test_rating_score_proportional_to_average():
    assert svc.calculate_rating_score({"reviews_count": 10, "rating_average": 5.0}) == 100.0
    assert svc.calculate_rating_score({"reviews_count": 10, "rating_average": 4.0}) == 80.0


# --------------------------------------------------------------------------- #
# calculate_experience_score
# --------------------------------------------------------------------------- #
def test_experience_score_tiers():
    assert svc.calculate_experience_score(80) == 100.0
    assert svc.calculate_experience_score(30) == 85.0
    assert svc.calculate_experience_score(10) == 70.0
    assert svc.calculate_experience_score(5) == 55.0
    assert svc.calculate_experience_score(4) == 40.0
    assert svc.calculate_experience_score(0) == 40.0


# --------------------------------------------------------------------------- #
# walk_interval_conflict / has_schedule_conflict
# --------------------------------------------------------------------------- #
def test_walk_interval_conflict_overlap_true():
    # Walk existente 14:00 (+45min). Novo passeio comeca 14:30 -> conflito.
    walk = Walk(id="w", tutor_id="t", pet_id="p", scheduled_date="2024-05-10T14:00:00", duration_minutes=45, price=1)
    assert svc.walk_interval_conflict(walk, datetime(2024, 5, 10, 14, 30), 45) is True


def test_walk_interval_conflict_buffer_blocks_adjacent():
    # Existente termina 14:45; buffer de 15min empurra para 15:00.
    # Novo passeio as 14:50 ainda colide pelo buffer.
    walk = Walk(id="w", tutor_id="t", pet_id="p", scheduled_date="2024-05-10T14:00:00", duration_minutes=45, price=1)
    assert svc.walk_interval_conflict(walk, datetime(2024, 5, 10, 14, 50), 30) is True


def test_walk_interval_conflict_far_apart_false():
    # Existente 14:00-14:45 (+buffer 15 = ate 15:00). Novo as 16:00 -> sem conflito.
    walk = Walk(id="w", tutor_id="t", pet_id="p", scheduled_date="2024-05-10T14:00:00", duration_minutes=45, price=1)
    assert svc.walk_interval_conflict(walk, datetime(2024, 5, 10, 16, 0), 45) is False


def test_walk_interval_conflict_unparseable_existing_date_false():
    walk = Walk(id="w", tutor_id="t", pet_id="p", scheduled_date="data-invalida", duration_minutes=45, price=1)
    assert svc.walk_interval_conflict(walk, datetime(2024, 5, 10, 14, 0), 45) is False


def test_has_schedule_conflict_no_scheduled_at_is_false():
    db = _db()
    req = MatchingWalkerRequest(city="salvador", neighborhood="pituba", scheduled_at=None)
    assert svc.has_schedule_conflict("walker-1", req, db) is False


def test_has_schedule_conflict_detects_overlap():
    db = _db()
    _walk(db, walker_id="walker-1", scheduled_date="2024-05-10T14:00:00", duration_minutes=45, status="Agendado")
    req = _request(scheduled_at="2024-05-10T14:30:00", duration_minutes=45)
    assert svc.has_schedule_conflict("walker-1", req, db) is True


def test_has_schedule_conflict_ignores_other_walker_and_finished():
    db = _db()
    # Mesmo horario, mas walker diferente.
    _walk(db, walker_id="outro", scheduled_date="2024-05-10T14:00:00", status="Agendado")
    # Mesmo walker mas status nao-ativo (Finalizado) -> nao entra no filtro.
    _walk(db, walker_id="walker-1", scheduled_date="2024-05-10T14:00:00", status="Finalizado")
    req = _request(scheduled_at="2024-05-10T14:10:00", duration_minutes=45)
    assert svc.has_schedule_conflict("walker-1", req, db) is False


def test_has_schedule_conflict_no_overlap_returns_false():
    db = _db()
    _walk(db, walker_id="walker-1", scheduled_date="2024-05-10T08:00:00", duration_minutes=45, status="Passeando agora")
    req = _request(scheduled_at="2024-05-10T18:00:00", duration_minutes=45)
    assert svc.has_schedule_conflict("walker-1", req, db) is False


# --------------------------------------------------------------------------- #
# calculate_availability_score
# --------------------------------------------------------------------------- #
def test_availability_score_zero_on_conflict():
    db = _db()
    profile = _profile(db, user_id="walker-1")
    _walk(db, walker_id="walker-1", scheduled_date="2024-05-10T14:00:00", duration_minutes=45)
    req = _request(scheduled_at="2024-05-10T14:30:00", duration_minutes=45)
    assert svc.calculate_availability_score(profile, req, db) == 0.0


def test_availability_score_full_when_scheduled_and_free():
    db = _db()
    profile = _profile(db, user_id="walker-1")
    profile.is_online = True  # WK-10: presença real entra no score (online = cheio)
    req = _request(scheduled_at="2024-05-10T14:30:00", duration_minutes=45)
    assert svc.calculate_availability_score(profile, req, db) == 100.0


def test_availability_score_immediate_request():
    db = _db()
    profile = _profile(db, user_id="walker-1")
    profile.is_online = True  # WK-10
    req = _request(scheduled_at=None)
    assert svc.calculate_availability_score(profile, req, db) == 80.0


def test_availability_score_offline_is_reduced():
    # WK-10: deixou de ser constante — offline pontua menos que online.
    db = _db()
    profile = _profile(db, user_id="walker-1")
    profile.is_online = False
    req = _request(scheduled_at=None)
    assert svc.calculate_availability_score(profile, req, db) == 40.0


# --------------------------------------------------------------------------- #
# get_eligible_walkers
# --------------------------------------------------------------------------- #
def test_eligible_only_active_and_active_as_walker():
    db = _db()
    active = _profile(db, user_id="ok", status="active", active_as_walker=True)
    _profile(db, user_id="pending", status="pending", active_as_walker=True)
    _profile(db, user_id="inativo", status="active", active_as_walker=False)
    result = svc.get_eligible_walkers(_request(), db)
    ids = [p.user_id for p in result]
    assert ids == [active.user_id]


def test_eligible_excludes_walker_in_different_city():
    db = _db()
    _profile(db, user_id="ok", city="salvador", state="pituba")
    _profile(db, user_id="fora", city="lauro de freitas", state="centro")
    result = svc.get_eligible_walkers(_request(city="salvador", neighborhood="pituba"), db)
    assert [p.user_id for p in result] == ["ok"]


def test_eligible_pet_tour_requires_vehicle():
    db = _db()
    com_carro = _profile(db, user_id="com-carro", has_vehicle=True)
    _profile(db, user_id="sem-carro", has_vehicle=False)
    result = svc.get_eligible_walkers(_request(modality="pet_tour"), db)
    assert [p.user_id for p in result] == [com_carro.user_id]


def test_eligible_standard_does_not_require_vehicle():
    db = _db()
    _profile(db, user_id="sem-carro", has_vehicle=False)
    result = svc.get_eligible_walkers(_request(modality="standard"), db)
    assert [p.user_id for p in result] == ["sem-carro"]


def test_eligible_excludes_suspended_risk_level():
    db = _db()
    # status suspended -> determine_risk_level retorna "suspended".
    _profile(db, user_id="susp", status="suspended", active_as_walker=True)
    # Mas o filtro de query exige status == "active"; suspended nem entra na query.
    # Para exercitar o ramo de risk_level "suspended" dentro do loop, usamos status
    # "blocked"? Tambem nao passa no filtro. Confirmamos entao que suspended nao volta.
    result = svc.get_eligible_walkers(_request(), db)
    assert result == []


def test_eligible_dedupes_by_cpf():
    db = _db()
    _profile(db, user_id="a", cpf="12345678900", created_at=datetime(2024, 1, 2))
    _profile(db, user_id="b", cpf="12345678900", created_at=datetime(2024, 1, 1))
    result = svc.get_eligible_walkers(_request(), db)
    # Ordenado por created_at desc; primeiro CPF visto vence (user a).
    assert [p.user_id for p in result] == ["a"]


def test_eligible_excludes_when_schedule_conflict():
    db = _db()
    _profile(db, user_id="ocupado")
    _walk(db, walker_id="ocupado", scheduled_date="2024-05-10T14:00:00", duration_minutes=45)
    req = _request(scheduled_at="2024-05-10T14:20:00", duration_minutes=45)
    assert svc.get_eligible_walkers(req, db) == []


def test_eligible_empty_when_no_profiles():
    db = _db()
    assert svc.get_eligible_walkers(_request(), db) == []
