from __future__ import annotations

import app.models  # noqa: F401 — registra modelos no Base.metadata

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.models.tutor_referral import TutorReferralConfig, TutorReferral


def _db():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    return sessionmaker(bind=eng)()


def test_config_defaults():
    db = _db()
    cfg = TutorReferralConfig(tenant_id="t1")
    db.add(cfg); db.commit(); db.refresh(cfg)
    assert cfg.enabled is False
    assert cfg.reward_type == "desconto"
    assert cfg.discount_kind == "percent"
    assert cfg.trigger_type == "primeiro_passeio_pago"
    assert cfg.trigger_n == 3
    assert cfg.same_reward_both_sides is True
    assert cfg.referrer_multiplier == 1.0


def test_referral_defaults_and_unique():
    db = _db()
    r = TutorReferral(id="r1", tenant_id="t1", referrer_user_id="u1", referral_code="TUT-ABC-123")
    db.add(r); db.commit(); db.refresh(r)
    assert r.status == "pending"
    assert r.reward_status == "not_eligible"
    assert r.completed_paid_walks_count == 0
