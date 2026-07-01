from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401 — registra mappers
from app.core.database import Base
from app.models.user import User
from app.models.walker_referral import WalkerReferral
from app.models.walker_earning import WalkerEarning, WE_ACCRUED
from app.services import walker_referrals as svc


def _db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[User.__table__, WalkerReferral.__table__, WalkerEarning.__table__],
    )
    return sessionmaker(bind=engine)()


def _referral(db, *, status="eligible_conv", reward_status="eligible", reward_amount=20.0):
    r = WalkerReferral(
        id="ref1", referrer_user_id="u_referrer", referred_user_id="u_referred",
        referred_name="Fulano", referred_phone="71999999999", referred_phone_normalized="5571999999999",
        city="Floripa", neighborhood="Centro", referral_code="AUM-ABC-123456",
        invite_link="x", status="converted", reward_status=reward_status,
        reward_amount=reward_amount, completed_walks_count=5, performance_status="neutral",
    )
    db.add(r)
    db.commit()
    return r


def test_payout_gated_off(monkeypatch):
    monkeypatch.delenv("WALKER_REFERRAL_PAYOUT_ENABLED", raising=False)
    db = _db()
    r = _referral(db)
    created = svc.pay_referral_rewards(db, r)
    db.commit()
    assert created is False
    assert db.query(WalkerEarning).count() == 0
    assert r.reward_status == "eligible"  # inalterado


def test_payout_creates_two_earnings_when_enabled(monkeypatch):
    monkeypatch.setenv("WALKER_REFERRAL_PAYOUT_ENABLED", "true")
    db = _db()
    r = _referral(db)
    created = svc.pay_referral_rewards(db, r)
    db.commit()
    assert created is True
    earnings = db.query(WalkerEarning).order_by(WalkerEarning.walk_id).all()
    assert len(earnings) == 2
    ids = {e.walk_id for e in earnings}
    assert ids == {"referral-ref1-referred", "referral-ref1-referrer"}
    for e in earnings:
        assert e.amount == 20.0
        assert e.gross == 20.0
        assert e.platform_amount == 0.0
        assert e.status == WE_ACCRUED
        assert e.payable_at is not None
    assert r.reward_status == "paid"


def test_payout_idempotent(monkeypatch):
    monkeypatch.setenv("WALKER_REFERRAL_PAYOUT_ENABLED", "true")
    db = _db()
    r = _referral(db)
    svc.pay_referral_rewards(db, r)
    db.commit()
    # segunda chamada: reward_status já 'paid' → no-op, sem terceira entrada
    created2 = svc.pay_referral_rewards(db, r)
    db.commit()
    assert created2 is False
    assert db.query(WalkerEarning).count() == 2


def test_payout_skips_referred_when_missing(monkeypatch):
    monkeypatch.setenv("WALKER_REFERRAL_PAYOUT_ENABLED", "true")
    db = _db()
    r = _referral(db)
    r.referred_user_id = None
    db.commit()
    svc.pay_referral_rewards(db, r)
    db.commit()
    assert db.query(WalkerEarning).count() == 1
    assert db.query(WalkerEarning).first().walk_id == "referral-ref1-referrer"


def test_notify_creates_two_reward_notifications(monkeypatch):
    from app.models.notification import Notification
    # evita I/O de push nos testes
    monkeypatch.setattr(
        "app.routes.notifications.send_push_for_notification_background",
        lambda *a, **k: None,
    )
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[User.__table__, WalkerReferral.__table__, Notification.__table__],
    )
    db = sessionmaker(bind=engine)()
    db.add(User(id="u_referrer", email="r@t.com", password_hash="x", role="walker", tenant_id="t1"))
    db.add(User(id="u_referred", email="d@t.com", password_hash="x", role="walker", tenant_id="t1"))
    r = WalkerReferral(
        id="ref1", referrer_user_id="u_referrer", referred_user_id="u_referred",
        referred_name="F", referred_phone="71999999999", referred_phone_normalized="5571999999999",
        city="Floripa", neighborhood="Centro", referral_code="AUM-ABC-123456",
        invite_link="x", status="converted", reward_status="paid",
        reward_amount=20.0, completed_walks_count=5, performance_status="neutral",
    )
    db.add(r)
    db.commit()

    svc.notify_referral_rewards(db, r)
    db.commit()

    notes = db.query(Notification).all()
    assert len(notes) == 2
    assert all(n.type == "reward_eligible" for n in notes)
    assert {n.user_id for n in notes} == {"u_referrer", "u_referred"}
    assert all(n.related_entity_type == "walker_referral" for n in notes)


def _db_with_walks():
    from app.models.walk import Walk
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[User.__table__, WalkerReferral.__table__, WalkerEarning.__table__, Walk.__table__],
    )
    return sessionmaker(bind=engine)(), Walk


def _mk_walk(Walk, wid, walker_id, status):
    return Walk(
        id=wid, tutor_id="t", walker_id=walker_id, tenant_id="t1", pet_id="p",
        scheduled_date="2026-06-12T14:00:00", duration_minutes=30, price=50.0,
        status="Finalizado", operational_status=status, walker_selection_mode="auto",
    )


def test_refresh_noop_when_no_approved_referral(monkeypatch):
    db, Walk = _db_with_walks()
    # referral existe mas NÃO aprovado
    r = WalkerReferral(
        id="ref1", referrer_user_id="u_referrer", referred_user_id="u_referred",
        referred_name="F", referred_phone="71999999999", referred_phone_normalized="5571999999999",
        city="C", neighborhood="N", referral_code="AUM-ABC-123456", invite_link="x",
        status="registered", reward_status="not_eligible", performance_status="neutral",
        completed_walks_count=0,
    )
    db.add(r); db.commit()
    svc.refresh_referred_walk_count(db, "u_referred")
    db.commit()
    assert db.query(WalkerReferral).get("ref1").status == "registered"


def test_refresh_converts_and_pays_at_five(monkeypatch):
    monkeypatch.setenv("WALKER_REFERRAL_PAYOUT_ENABLED", "true")
    monkeypatch.setattr(svc, "notify_referral_rewards", lambda db, r: None)  # isola notificação
    db, Walk = _db_with_walks()
    r = WalkerReferral(
        id="ref1", referrer_user_id="u_referrer", referred_user_id="u_referred",
        referred_name="F", referred_phone="71999999999", referred_phone_normalized="5571999999999",
        city="C", neighborhood="N", referral_code="AUM-ABC-123456", invite_link="x",
        status="approved", reward_status="pending", reward_amount=20.0,
        performance_status="neutral", completed_walks_count=0,
    )
    db.add(r)
    for i in range(5):
        db.add(_mk_walk(Walk, f"w{i}", "u_referred", "ride_completed"))
    db.commit()

    svc.refresh_referred_walk_count(db, "u_referred")
    db.commit()

    r2 = db.query(WalkerReferral).get("ref1")
    assert r2.completed_walks_count == 5
    assert r2.status == "converted"
    assert r2.reward_status == "paid"
    assert db.query(WalkerEarning).count() == 2


def test_refresh_below_threshold_no_conversion(monkeypatch):
    db, Walk = _db_with_walks()
    r = WalkerReferral(
        id="ref1", referrer_user_id="u_referrer", referred_user_id="u_referred",
        referred_name="F", referred_phone="71999999999", referred_phone_normalized="5571999999999",
        city="C", neighborhood="N", referral_code="AUM-ABC-123456", invite_link="x",
        status="approved", reward_status="pending", reward_amount=20.0,
        performance_status="neutral", completed_walks_count=0,
    )
    db.add(r)
    for i in range(3):
        db.add(_mk_walk(Walk, f"w{i}", "u_referred", "ride_completed"))
    db.commit()

    svc.refresh_referred_walk_count(db, "u_referred")
    db.commit()

    r2 = db.query(WalkerReferral).get("ref1")
    assert r2.completed_walks_count == 3
    assert r2.status == "approved"
    assert db.query(WalkerEarning).count() == 0


def test_refresh_idempotent(monkeypatch):
    monkeypatch.setenv("WALKER_REFERRAL_PAYOUT_ENABLED", "true")
    monkeypatch.setattr(svc, "notify_referral_rewards", lambda db, r: None)
    db, Walk = _db_with_walks()
    r = WalkerReferral(
        id="ref1", referrer_user_id="u_referrer", referred_user_id="u_referred",
        referred_name="F", referred_phone="71999999999", referred_phone_normalized="5571999999999",
        city="C", neighborhood="N", referral_code="AUM-ABC-123456", invite_link="x",
        status="approved", reward_status="pending", reward_amount=20.0,
        performance_status="neutral", completed_walks_count=0,
    )
    db.add(r)
    for i in range(5):
        db.add(_mk_walk(Walk, f"w{i}", "u_referred", "ride_completed"))
    db.commit()

    svc.refresh_referred_walk_count(db, "u_referred")
    db.commit()
    svc.refresh_referred_walk_count(db, "u_referred")  # 2ª vez: já converted/paid
    db.commit()

    assert db.query(WalkerEarning).count() == 2  # sem duplicar


def test_invite_link_is_full_public_url():
    import re
    from app.services.walker_referrals import _build_invite_link
    link = _build_invite_link("AUM-ABC-123456")
    assert link == "https://app.aumigaowalk.com.br/referral/AUM-ABC-123456"
