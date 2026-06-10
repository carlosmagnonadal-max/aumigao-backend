"""Testes do trust exposto no payload do matching (visivel ao tutor).

Foco: matched_walker_payload (e o payload publico de rank_walkers) inclui a chave
"trust" no contrato { walker_user_id, seals, certifications, level, metrics }.

NAO gateia por flag aqui: a EXIBICAO e gateada no front (decisao da spec). Este
teste so afirma que o backend EXPOE o trust.

Padrao do repo: SQLite em memoria, criando SO as tabelas usadas (sem app.main, sem
alembic, sem banco real). Alem de WalkerProfile + Walk + WalkerReview (matching),
compute_walker_trust consulta Complaint (incidentes criticos) e User (identidade).
"""
from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.complaint import Complaint
from app.models.user import User
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
            Complaint.__table__,
            User.__table__,
        ],
    )
    return sessionmaker(bind=engine)()


def _seed_walker(db, *, user_id="walker-1"):
    user = User(
        id=user_id,
        email=f"{user_id}@example.com",
        password_hash="x",
        full_name="Passeador Trust",
        role="walker",
    )
    db.add(user)
    profile = WalkerProfile(
        id=f"wp-{user_id}",
        user_id=user_id,
        full_name="Passeador Trust",
        cpf="12345678900",
        phone="71999999999",
        city="salvador",
        state="pituba",
        status="active",
        active_as_walker=True,
        created_at=datetime(2024, 1, 1, 12, 0, 0),
    )
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


def _request(**kwargs) -> MatchingWalkerRequest:
    base = {"city": "salvador", "neighborhood": "pituba"}
    base.update(kwargs)
    return MatchingWalkerRequest(**base)


# --------------------------------------------------------------------------- #
# matched_walker_payload inclui trust no contrato esperado
# --------------------------------------------------------------------------- #
def test_matched_walker_payload_includes_trust():
    db = _db()
    profile = _seed_walker(db, user_id="walker-1")
    payload = svc.matched_walker_payload(profile, _request(), db)

    assert "trust" in payload
    trust = payload["trust"]
    # walker_user_id correto.
    assert trust["walker_user_id"] == "walker-1"
    # seals com as 3 chaves booleanas do contrato.
    assert set(trust["seals"].keys()) == {
        "cadastro_verificado",
        "identidade_verificada",
        "passeador_verificado",
    }
    assert all(isinstance(v, bool) for v in trust["seals"].values())
    # level nos rotulos novos.
    assert trust["level"] in {"Bronze", "Prata", "Ouro", "Diamante"}
    # certifications e uma lista de itens key/label/icon/granted.
    assert isinstance(trust["certifications"], list)
    for cert in trust["certifications"]:
        assert {"key", "label", "icon", "granted"} <= set(cert.keys())
    # metrics presente (informativo).
    assert isinstance(trust["metrics"], dict)


def test_matched_walker_payload_trust_matches_service():
    """O trust embutido e exatamente o de compute_walker_trust (sem divergencia)."""
    from app.services.walker_trust_service import compute_walker_trust

    db = _db()
    profile = _seed_walker(db, user_id="walker-2")
    payload = svc.matched_walker_payload(profile, _request(), db)
    assert payload["trust"] == compute_walker_trust(db, "walker-2")


# --------------------------------------------------------------------------- #
# rank_walkers expoe trust no payload PUBLICO (o que o tutor recebe)
# --------------------------------------------------------------------------- #
def test_rank_walkers_public_payload_includes_trust():
    db = _db()
    _seed_walker(db, user_id="walker-1")
    result = svc.rank_walkers(_request(), db, debug=False)

    walkers = result["top_recommended"] + result["other_options"]
    assert walkers, "esperava ao menos um passeador elegivel"
    for walker in walkers:
        assert "trust" in walker
        assert walker["trust"]["walker_user_id"]
        assert set(walker["trust"]["seals"].keys()) == {
            "cadastro_verificado",
            "identidade_verificada",
            "passeador_verificado",
        }
        assert walker["trust"]["level"] in {"Bronze", "Prata", "Ouro", "Diamante"}
