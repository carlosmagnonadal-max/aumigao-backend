from __future__ import annotations

import json

from sqlalchemy.orm import Session

from app.models.tutor_referral import TutorReferral


def _reward_phrase(reward_type: str | None) -> str:
    if reward_type == "passeio_gratis":
        return "passeio(s) grátis 🐾"
    if reward_type == "credito":
        return "passeios de crédito na sua assinatura"
    return "um cupom de desconto"


def notify_tutor_referral_rewards(db: Session, referral: TutorReferral) -> None:
    """Notifica (push) os 2 lados quando a recompensa da indicação é concedida."""
    from app.routes.notifications import NotificationCreate, _create_notification

    snap = json.loads(referral.reward_snapshot_json) if referral.reward_snapshot_json else {}
    phrase = _reward_phrase(snap.get("reward_type"))

    targets = [
        (referral.referrer_user_id, "🎉 Sua indicação converteu",
         f"Seu convidado entrou! Você ganhou {phrase}."),
        (referral.referred_user_id, "🎉 Você ganhou uma recompensa",
         f"Bem-vindo! Você ganhou {phrase} por entrar pela indicação."),
    ]
    for user_id, title, message in targets:
        if not user_id:
            continue
        _create_notification(db, NotificationCreate(
            tenant_id=referral.tenant_id,
            user_id=user_id,
            user_role="tutor",
            title=title,
            message=message,
            type="reward_eligible",
            related_entity_type="tutor_referral",
            related_entity_id=referral.id,
            metadata={"reward_type": snap.get("reward_type")},
        ))
