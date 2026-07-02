"""FIX 9 (P2) — WalkerReferral e o WalkerEarning de referral carregam tenant_id.

Antes, o earning de referral gravava tenant_id=None, quebrando o isolamento
multi-tenant ao ligar WALKER_REFERRAL_PAYOUT_ENABLED. Agora a indicação grava o
tenant do referrer e o earning herda esse tenant_id.
"""
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.core.database import Base
from app.models.user import User
from app.models.walker_profile import WalkerProfile
from app.models.walker_referral import WalkerReferral
from app.models.walker_earning import WalkerEarning
from app.models.tenant import Tenant
from app.schemas.walker_referral import WalkerReferralCreate
from app.services import walker_referrals as svc
from app.services.tenant_seed_service import DEFAULT_TENANT_SLUG

TENANT_ID = "t-ref"


def _db():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    return sessionmaker(bind=eng)()


def _walker(db, uid):
    db.add(User(id=uid, email=f"{uid}@r.com", password_hash="x", role="walker", tenant_id=TENANT_ID))
    db.add(WalkerProfile(id=f"wp-{uid}", user_id=uid, status="approved"))
    db.commit()
    return db.get(User, uid)


def test_create_walker_referral_stores_tenant_id(monkeypatch):
    db = _db()
    db.add(Tenant(id=TENANT_ID, name="Aumigao", slug=DEFAULT_TENANT_SLUG, status="active", plan="business"))
    db.commit()
    referrer = _walker(db, "ref-1")

    # ensure_can_refer pode exigir estados; simplifica retornando True.
    monkeypatch.setattr(svc, "ensure_can_refer", lambda u, d: None)

    payload = WalkerReferralCreate(
        referred_name="Fulano", referred_phone="11999998888",
        city="SP", neighborhood="Centro",
    )
    referral = svc.create_walker_referral(payload, referrer, db)
    assert referral.tenant_id == TENANT_ID


def test_referral_earning_inherits_tenant_id(monkeypatch):
    db = _db()
    db.add(Tenant(id=TENANT_ID, name="Aumigao", slug=DEFAULT_TENANT_SLUG, status="active", plan="business"))
    db.commit()
    referrer = _walker(db, "ref-2")
    referred = _walker(db, "red-2")

    monkeypatch.setattr(svc, "_referral_payout_enabled", lambda: True)

    referral = WalkerReferral(
        id=str(uuid4()),
        tenant_id=TENANT_ID,
        referrer_user_id=referrer.id,
        referred_user_id=referred.id,
        referred_name="X", referred_phone="11999997777",
        referred_phone_normalized="5511999997777",
        city="SP", neighborhood="Centro",
        referral_code="AUM-REF-2",
        status="approved", reward_status="eligible", reward_amount=20.0,
    )
    db.add(referral); db.commit()

    created = svc.pay_referral_rewards(db, referral)
    db.commit()
    assert created is True

    earnings = db.query(WalkerEarning).filter(WalkerEarning.walk_id.like("referral-%")).all()
    assert len(earnings) == 2
    # Isolamento: todo earning de referral carrega o tenant do referrer, não None.
    assert all(e.tenant_id == TENANT_ID for e in earnings)
