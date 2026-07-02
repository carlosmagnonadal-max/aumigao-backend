"""BG-7 — selo antecedentes_verificados exposto ao tutor no matching.

Regra:
- antecedentes_verificados = True apenas quando:
  (a) flag 'background_checks' do tenant esta ON, E
  (b) walker_profile.background_check_status == "verified".
- Caso contrario: False.
- NUNCA vazar dados das certidoes (somente o booleano).
- Sem N+1: background_check_status vem do profile ja buscado.

Cobre:
- matched_walker_payload e rank_walkers com flag OFF -> False.
- matched_walker_payload e rank_walkers com flag ON + status != "verified" -> False.
- matched_walker_payload e rank_walkers com flag ON + status == "verified" -> True.
- Certidoes nao aparecem no payload publico (garante sem vazamento de PII).

Padrao: SQLite in-memory, mesmas tabelas de test_matching_trust.py.
"""
from datetime import datetime
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.complaint import Complaint
from app.models.tenant import Tenant, TenantFeature
from app.models.user import User
from app.models.walk import Walk
from app.models.walker_background_certificate import WalkerBackgroundCertificate
from app.models.walker_profile import WalkerProfile
from app.models.walker_review import WalkerReview
from app.schemas.matching import MatchingWalkerRequest
from app.services import matching_service as svc

TENANT_ID = "t-selo"
WALKER_USER_ID = "walker-selo-1"


def _db(*, flag_on: bool = False, bg_status: str = "none"):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[
            Tenant.__table__,
            TenantFeature.__table__,
            WalkerProfile.__table__,
            Walk.__table__,
            WalkerReview.__table__,
            WalkerBackgroundCertificate.__table__,
            Complaint.__table__,
            User.__table__,
        ],
    )
    db = sessionmaker(bind=engine)()

    db.add(Tenant(
        id=TENANT_ID, name="Aumigao", slug="aumigao",
        status="active", plan="business",
    ))
    if flag_on:
        db.add(TenantFeature(
            id=str(uuid4()),
            tenant_id=TENANT_ID,
            feature_key="background_checks",
            enabled=True,
        ))

    db.add(User(
        id=WALKER_USER_ID,
        email="walker-selo@example.com",
        password_hash="x",
        full_name="Passeador Selo",
        role="walker",
        tenant_id=TENANT_ID,
    ))
    db.add(WalkerProfile(
        id=f"wp-{WALKER_USER_ID}",
        user_id=WALKER_USER_ID,
        full_name="Passeador Selo",
        cpf="11144477735",
        phone="71999990000",
        city="salvador",
        state="pituba",
        status="active",
        active_as_walker=True,
        background_check_status=bg_status,
        created_at=datetime(2024, 1, 1, 12, 0, 0),
    ))
    db.commit()
    return db


def _request() -> MatchingWalkerRequest:
    return MatchingWalkerRequest(city="salvador", neighborhood="pituba", tenant_id=TENANT_ID)


# ---------------------------------------------------------------------------
# matched_walker_payload — antecedentes_verificados
# ---------------------------------------------------------------------------

def test_payload_flag_off_antecedentes_false():
    """Flag OFF -> antecedentes_verificados = False, independente do bg_status."""
    db = _db(flag_on=False, bg_status="verified")
    profile = db.query(WalkerProfile).first()

    payload = svc.matched_walker_payload(profile, _request(), db)

    assert "antecedentes_verificados" in payload, (
        "campo antecedentes_verificados ausente no matched_walker_payload"
    )
    assert payload["antecedentes_verificados"] is False


def test_payload_flag_on_nao_verified_antecedentes_false():
    """Flag ON mas status != verified -> False."""
    db = _db(flag_on=True, bg_status="submitted")
    profile = db.query(WalkerProfile).first()

    payload = svc.matched_walker_payload(profile, _request(), db)
    assert payload["antecedentes_verificados"] is False


def test_payload_flag_on_verified_antecedentes_true():
    """Flag ON + status verified -> True."""
    db = _db(flag_on=True, bg_status="verified")
    profile = db.query(WalkerProfile).first()

    payload = svc.matched_walker_payload(profile, _request(), db)
    assert payload["antecedentes_verificados"] is True


def test_payload_nao_vaza_certidoes():
    """Certidoes (dados sensiveis) NAO devem aparecer no payload publico."""
    db = _db(flag_on=True, bg_status="verified")
    profile = db.query(WalkerProfile).first()

    payload = svc.matched_walker_payload(profile, _request(), db)

    # Chaves proibidas no payload publico
    assert "background_certificates" not in payload
    assert "background_consent_at" not in payload
    assert "background_consent_version" not in payload
    assert "background_verified_at" not in payload


# ---------------------------------------------------------------------------
# rank_walkers — antecedentes_verificados no payload publico
# ---------------------------------------------------------------------------

def test_rank_walkers_public_flag_off_antecedentes_false():
    """No payload publico do rank_walkers, flag OFF -> antecedentes_verificados = False."""
    db = _db(flag_on=False, bg_status="verified")

    result = svc.rank_walkers(_request(), db, debug=False)
    walkers = result["top_recommended"] + result["other_options"]
    assert walkers, "esperava ao menos um passeador elegivel"
    for w in walkers:
        assert w["antecedentes_verificados"] is False


def test_rank_walkers_public_flag_on_verified_antecedentes_true():
    """No payload publico do rank_walkers, flag ON + verified -> True."""
    db = _db(flag_on=True, bg_status="verified")

    result = svc.rank_walkers(_request(), db, debug=False)
    walkers = result["top_recommended"] + result["other_options"]
    assert walkers, "esperava ao menos um passeador elegivel"
    for w in walkers:
        assert w["antecedentes_verificados"] is True


def test_rank_walkers_nao_vaza_certidoes():
    """Certidoes nao devem aparecer no payload publico de rank_walkers."""
    db = _db(flag_on=True, bg_status="verified")

    result = svc.rank_walkers(_request(), db, debug=False)
    walkers = result["top_recommended"] + result["other_options"]
    for w in walkers:
        assert "background_certificates" not in w
        assert "background_consent_at" not in w


# ---------------------------------------------------------------------------
# Sem N+1: background_check_status vem do profile (sem query extra de certidoes)
# ---------------------------------------------------------------------------

def test_payload_usa_status_do_profile_sem_query_certs():
    """antecedentes_verificados deve derivar de profile.background_check_status
    ja carregado — nao de uma nova query de certidoes.
    O status do profile e "verified" mas nao ha WalkerBackgroundCertificate na base;
    o campo ainda deve ser True (pois vai pelo profile, nao re-query).
    """
    db = _db(flag_on=True, bg_status="verified")
    # Confirma: nenhuma certidao na base (so o status do profile conta aqui)
    assert db.query(WalkerBackgroundCertificate).count() == 0

    profile = db.query(WalkerProfile).first()
    payload = svc.matched_walker_payload(profile, _request(), db)
    # Se o campo viesse de re-query de certs, seria False (nenhuma cert).
    assert payload["antecedentes_verificados"] is True
