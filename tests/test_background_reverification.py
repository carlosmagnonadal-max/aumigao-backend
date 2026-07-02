"""BG-reverificacao — cron de expiracao periodica de certidoes de antecedentes.

Regra: certidao `validated` com validated_at mais antigo que N dias (default 365,
env BACKGROUND_CERT_MAX_AGE_DAYS) vira `expired`, o status agregado do profile e
recalculado (deixa de ser `verified` -> reabre a pendencia/gate) e o passeador
recebe notificacao pra reemitir (certidao e gratis).

- So age em tenants com a flag `background_checks` ON (sem flag = no-op).
- Idempotente: rodar 2x no dia nao duplica notificacao (so notifica na transicao
  validated -> expired).
"""
from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.core.database import Base
from app.models.notification import Notification
from app.models.tenant import Tenant, TenantFeature
from app.models.user import User
from app.models.walker_background_certificate import WalkerBackgroundCertificate
from app.models.walker_profile import WalkerProfile
from app.services import background_reverification_service as svc

TENANT_ID = "t-rev"


def _build(*, flag_on: bool = True):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(Tenant(id=TENANT_ID, name="Aumigao", slug="aumigao", status="active", plan="business"))
    db.add(User(id="wu-1", email="walker@correio.com", password_hash="x", role="walker", full_name="Passeador", tenant_id=TENANT_ID))
    prof = WalkerProfile(
        id="prof-1", user_id="wu-1", full_name="Passeador", cpf="52998224725",
        city="Sao Paulo", state="SP", status="active",
        background_check_status="verified", background_verified_at=datetime.utcnow(),
        created_at=datetime.utcnow(),
    )
    db.add(prof)
    if flag_on:
        db.add(TenantFeature(id=str(uuid4()), tenant_id=TENANT_ID, feature_key="background_checks", enabled=True))
    db.commit()
    return db, prof


def _cert(db, cert_type, status, validated_days_ago=None, uf=None):
    validated_at = None
    if validated_days_ago is not None:
        validated_at = datetime.utcnow() - timedelta(days=validated_days_ago)
    cert = WalkerBackgroundCertificate(
        id=str(uuid4()), walker_profile_id="prof-1", cert_type=cert_type,
        issuer_uf=uf, cert_number=f"{cert_type}-1", status=status, validated_at=validated_at,
    )
    db.add(cert)
    db.commit()
    return cert


def _walker_notifs(db):
    return db.query(Notification).filter(Notification.type == "background_reverification").all()


# ------------------------------------------------------------------- expira velha
def test_expires_stale_validated_and_reopens_gate(monkeypatch):
    monkeypatch.setenv("BACKGROUND_CERT_MAX_AGE_DAYS", "365")
    db, prof = _build()
    old_pf = _cert(db, "pf", "validated", validated_days_ago=400)
    _cert(db, "tj", "validated", validated_days_ago=10, uf="SP")

    result = svc.sweep_stale_certificates(db)
    db.refresh(old_pf)
    db.refresh(prof)

    assert result["expired"] == 1
    assert old_pf.status == "expired"
    # Status agregado recalculado: deixa de ser verified.
    assert prof.background_check_status != "verified"
    assert prof.background_verified_at is None
    # Passeador notificado a reemitir.
    assert len(_walker_notifs(db)) == 1


def test_recent_validated_untouched(monkeypatch):
    monkeypatch.setenv("BACKGROUND_CERT_MAX_AGE_DAYS", "365")
    db, prof = _build()
    pf = _cert(db, "pf", "validated", validated_days_ago=100)
    tj = _cert(db, "tj", "validated", validated_days_ago=100, uf="SP")

    result = svc.sweep_stale_certificates(db)
    db.refresh(pf)
    db.refresh(tj)
    db.refresh(prof)
    assert result["expired"] == 0
    assert pf.status == "validated" and tj.status == "validated"
    assert prof.background_check_status == "verified"
    assert _walker_notifs(db) == []


# --------------------------------------------------------------------- idempotente
def test_idempotent_no_duplicate_notification(monkeypatch):
    monkeypatch.setenv("BACKGROUND_CERT_MAX_AGE_DAYS", "365")
    db, prof = _build()
    _cert(db, "pf", "validated", validated_days_ago=400)

    r1 = svc.sweep_stale_certificates(db)
    r2 = svc.sweep_stale_certificates(db)
    assert r1["expired"] == 1
    assert r2["expired"] == 0  # ja expirada, nada a fazer
    assert len(_walker_notifs(db)) == 1  # notificou 1x so


# ------------------------------------------------------------------ flag OFF = noop
def test_flag_off_is_noop(monkeypatch):
    monkeypatch.setenv("BACKGROUND_CERT_MAX_AGE_DAYS", "365")
    db, prof = _build(flag_on=False)
    pf = _cert(db, "pf", "validated", validated_days_ago=400)

    result = svc.sweep_stale_certificates(db)
    db.refresh(pf)
    assert result["expired"] == 0
    assert pf.status == "validated"  # flag OFF -> nao mexe
    assert _walker_notifs(db) == []


# ----------------------------------------------------------- default de 365 dias
def test_default_max_age_is_365(monkeypatch):
    monkeypatch.delenv("BACKGROUND_CERT_MAX_AGE_DAYS", raising=False)
    db, prof = _build()
    # 300 dias < 365 default -> nao expira.
    pf = _cert(db, "pf", "validated", validated_days_ago=300)
    _cert(db, "tj", "validated", validated_days_ago=10, uf="SP")
    result = svc.sweep_stale_certificates(db)
    db.refresh(pf)
    assert result["expired"] == 0
    assert pf.status == "validated"


def test_configurable_max_age(monkeypatch):
    monkeypatch.setenv("BACKGROUND_CERT_MAX_AGE_DAYS", "90")
    db, prof = _build()
    pf = _cert(db, "pf", "validated", validated_days_ago=100)  # > 90
    result = svc.sweep_stale_certificates(db)
    db.refresh(pf)
    assert result["expired"] == 1
    assert pf.status == "expired"
