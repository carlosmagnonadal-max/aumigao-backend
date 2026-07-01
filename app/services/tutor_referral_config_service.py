from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.tutor_referral import TutorReferralConfig

REWARD_TYPES = {"desconto", "passeio_gratis", "credito"}
DISCOUNT_KINDS = {"percent", "fixed"}
TRIGGER_TYPES = {"primeiro_passeio_pago", "n_passeios", "no_cadastro"}
_NON_NEGATIVE_FIELDS = {"discount_value", "free_walks_count", "credit_walks",
                        "referrer_multiplier", "referred_multiplier"}


def get_or_create_tutor_referral_config(db: Session, tenant_id: str) -> TutorReferralConfig:
    config = (
        db.query(TutorReferralConfig)
        .filter(TutorReferralConfig.tenant_id == tenant_id)
        .first()
    )
    if not config:
        # Meio-termo "pré-preenchido + 1 clique" (decisão do fundador): o config nasce
        # com uma recompensa padrão sensata mas DESLIGADO — o tenant só liga o master.
        # Não força custo em ninguém (enabled=False); só reduz a fricção de configurar.
        config = TutorReferralConfig(
            tenant_id=tenant_id,
            enabled=False,
            reward_type="desconto",
            discount_kind="fixed",
            discount_value=20.0,
            same_reward_both_sides=True,
            trigger_type="primeiro_passeio_pago",
        )
        db.add(config)
        db.flush()  # flush, não commit — o caller comita
    return config


def validate_config_update(values: dict) -> None:
    """Valida um payload parcial de update. Levanta HTTPException(422) se inválido."""
    if "reward_type" in values and values["reward_type"] not in REWARD_TYPES:
        raise HTTPException(status_code=422, detail="reward_type inválido.")
    if "discount_kind" in values and values["discount_kind"] not in DISCOUNT_KINDS:
        raise HTTPException(status_code=422, detail="discount_kind inválido.")
    if "trigger_type" in values and values["trigger_type"] not in TRIGGER_TYPES:
        raise HTTPException(status_code=422, detail="trigger_type inválido.")
    for field in _NON_NEGATIVE_FIELDS:
        if field in values and float(values[field]) < 0:
            raise HTTPException(status_code=422, detail=f"{field} não pode ser negativo.")
    if "trigger_n" in values and int(values["trigger_n"]) < 1:
        raise HTTPException(status_code=422, detail="trigger_n deve ser ≥ 1.")
