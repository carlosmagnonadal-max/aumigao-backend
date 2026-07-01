"""Dispatcher de recompensas do programa de indicação tutor→tutor (cunha 4).

Veículos implementados:
- desconto    → Coupon com discount_type fixed|percent
- passeio_gratis → Coupon com discount_type percent 100 % (is_referral_gift=True)
- credito     → créditos na assinatura ativa; retidos em held_credits_json se sem assinatura
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import update as sa_update
from sqlalchemy.orm import Session

from app.models.coupon import Coupon, DISCOUNT_FIXED, DISCOUNT_PERCENT
from app.models.tutor_referral import TutorReferral
from app.models.recurring_plan import TutorSubscription

COUPON_VALIDITY_DAYS = 90


def _sides(snapshot: dict) -> list[tuple[str, float]]:
    """Retorna (side_label, multiplier) para cada lado que recebe recompensa."""
    return [
        ("referrer", float(snapshot.get("referrer_multiplier", 1.0))),
        ("referred", float(snapshot.get("referred_multiplier", 1.0))),
    ]


_SIDE_ABBREV = {"referrer": "RER", "referred": "RED"}


def _coupon_code(referral_id: str, side: str) -> str:
    abbrev = _SIDE_ABBREV.get(side, side[:3].upper())
    return f"TREF-{referral_id[:8].upper()}-{abbrev}"


def _make_coupon(
    db: Session,
    tenant_id: str,
    referral_id: str,
    side: str,
    discount_type: str,
    discount_value: float,
    uses: int,
    is_gift: bool,
) -> None:
    """Insere cupom somente se ainda não existir (idempotência via código único)."""
    code = _coupon_code(referral_id, side)
    existing = (
        db.query(Coupon)
        .filter(Coupon.tenant_id == tenant_id, Coupon.code == code)
        .first()
    )
    if existing:
        return
    db.add(
        Coupon(
            tenant_id=tenant_id,
            code=code,
            discount_type=discount_type,
            discount_value=discount_value,
            max_uses=max(1, uses),
            max_uses_per_user=max(1, uses),
            active=True,
            valid_until=datetime.utcnow() + timedelta(days=COUPON_VALIDITY_DAYS),
            is_referral_gift=is_gift,
        )
    )


def grant_reward(db: Session, referral: TutorReferral) -> None:
    """Despacha a recompensa correta com base no snapshot congelado no momento da elegibilidade.

    Idempotente: chamadas repetidas não criam cupons duplicados nem alteram o status
    se já estiver em 'granted'.
    """
    if not referral.reward_snapshot_json:
        return

    if referral.reward_status == "granted":
        return  # idempotência: não reconceder (protege o crédito do += duplo)

    snap = json.loads(referral.reward_snapshot_json)
    rtype = snap.get("reward_type")

    for side, mult in _sides(snap):
        if rtype == "desconto":
            kind = (
                DISCOUNT_FIXED
                if snap.get("discount_kind") == "fixed"
                else DISCOUNT_PERCENT
            )
            value = float(snap.get("discount_value", 0.0)) * mult
            _make_coupon(
                db, referral.tenant_id, referral.id, side,
                discount_type=kind, discount_value=value, uses=1, is_gift=False,
            )

        elif rtype == "passeio_gratis":
            uses = int(round(float(snap.get("free_walks_count", 1)) * mult)) or 1
            _make_coupon(
                db, referral.tenant_id, referral.id, side,
                discount_type=DISCOUNT_PERCENT, discount_value=100.0, uses=uses, is_gift=True,
            )

        elif rtype == "credito":
            _uid_field = "referrer_user_id" if side == "referrer" else "referred_user_id"
            _grant_credit(db, referral, side, _uid_field, snap, mult)

    referral.reward_status = "granted"
    db.commit()


def _active_subscription(db: Session, tenant_id: str, tutor_id: str):
    return (
        db.query(TutorSubscription)
        .filter(
            TutorSubscription.tenant_id == tenant_id,
            TutorSubscription.tutor_id == tutor_id,
            TutorSubscription.status == "active",
        )
        .first()
    )


def _add_ledger_bonus(db: Session, subscription, credits: int) -> None:
    try:
        from app.models.credit_ledger import CreditLedgerEntry
        entry = CreditLedgerEntry(
            id=str(uuid4()), tenant_id=subscription.tenant_id, subscription_id=subscription.id,
            event_type="referral_bonus_granted", credits_count=credits,
            unit_value=0.0, total_value=0.0,
        )
        db.add(entry)
    except Exception:
        pass  # ledger contábil best-effort; nunca quebra a concessão


def _grant_credit(db: Session, referral: TutorReferral, side: str, uid_field: str,
                  snap: dict, mult: float) -> None:
    credits = int(round(float(snap.get("credit_walks", 0)) * mult))
    if credits <= 0:
        return
    tutor_id = getattr(referral, uid_field, None)
    if not tutor_id:
        return
    sub = _active_subscription(db, referral.tenant_id, tutor_id)
    if sub is not None:
        db.execute(
            sa_update(TutorSubscription)
            .where(TutorSubscription.id == sub.id)
            .values(credits_remaining=TutorSubscription.credits_remaining + credits,
                    updated_at=datetime.utcnow())
        )
        _add_ledger_bonus(db, sub, credits)
    else:
        held = json.loads(referral.held_credits_json) if referral.held_credits_json else {}
        held[side] = credits
        referral.held_credits_json = json.dumps(held)


def apply_held_credit_on_subscription(db: Session, subscription) -> None:
    refs = (
        db.query(TutorReferral)
        .filter(TutorReferral.tenant_id == subscription.tenant_id,
                TutorReferral.held_credits_json.isnot(None))
        .all()
    )
    total = 0
    for ref in refs:
        held = json.loads(ref.held_credits_json) if ref.held_credits_json else {}
        changed = False
        for side, uid_field in (("referrer", "referrer_user_id"), ("referred", "referred_user_id")):
            if held.get(side) and getattr(ref, uid_field, None) == subscription.tutor_id:
                total += int(held[side])
                held[side] = 0
                changed = True
        if changed:
            ref.held_credits_json = json.dumps(held)
    if total > 0:
        db.execute(
            sa_update(TutorSubscription)
            .where(TutorSubscription.id == subscription.id)
            .values(credits_remaining=TutorSubscription.credits_remaining + total,
                    updated_at=datetime.utcnow())
        )
        _add_ledger_bonus(db, subscription, total)
