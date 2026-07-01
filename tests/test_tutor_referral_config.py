from __future__ import annotations

import app.models  # noqa: F401

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.services import tutor_referral_config_service as svc


def _db():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    return sessionmaker(bind=eng)()


def test_get_or_create_is_idempotent():
    db = _db()
    a = svc.get_or_create_tutor_referral_config(db, "t1"); db.flush()
    b = svc.get_or_create_tutor_referral_config(db, "t1")
    assert a.id == b.id
    assert a.enabled is False


def test_new_config_is_prefilled_but_disabled():
    """Meio-termo (b): recompensa padrão pré-preenchida, mas DESLIGADO (1 clique p/ ligar)."""
    db = _db()
    cfg = svc.get_or_create_tutor_referral_config(db, "novo")
    assert cfg.enabled is False              # não força custo
    assert cfg.reward_type == "desconto"
    assert cfg.discount_kind == "fixed"
    assert cfg.discount_value == 20.0
    assert cfg.trigger_type == "primeiro_passeio_pago"


def test_validate_rejects_bad_reward_type():
    with pytest.raises(HTTPException) as e:
        svc.validate_config_update({"reward_type": "banana"})
    assert e.value.status_code == 422


def test_validate_rejects_negative_value():
    with pytest.raises(HTTPException):
        svc.validate_config_update({"discount_value": -5})


def test_validate_rejects_trigger_n_zero():
    with pytest.raises(HTTPException):
        svc.validate_config_update({"trigger_n": 0})


def test_validate_accepts_good_payload():
    svc.validate_config_update(
        {"reward_type": "credito", "credit_walks": 2, "trigger_type": "n_passeios", "trigger_n": 3}
    )  # não levanta
