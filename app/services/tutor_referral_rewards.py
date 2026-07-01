"""Dispatcher de recompensas do programa de indicação tutor→tutor (cunha 4).

Veículos implementados:
- desconto    → Coupon com discount_type fixed|percent
- passeio_gratis → Coupon com discount_type percent 100 % (is_referral_gift=True)
- credito     → no-op (Tarefa 4)
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.models.coupon import Coupon, DISCOUNT_FIXED, DISCOUNT_PERCENT
from app.models.tutor_referral import TutorReferral

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
            pass  # Tarefa 4 — crédito closed-loop no ledger

    referral.reward_status = "granted"
    db.commit()
