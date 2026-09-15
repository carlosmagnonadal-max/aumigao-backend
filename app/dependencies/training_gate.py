"""Trava da Capacitação do passeador (S2) — mesmo molde de dependencies/legal_gate.py.

403 com o shape:
    {"detail": {"code": "training_required", "message": "...", "required_version": "<versão>"}}

Decisão de bloquear vem de services/walker_training_policy (D2: só com conteúdo vet_approved,
ENFORCED_FROM e fim da carência). Em "warn" apenas registra log e deixa passar.
"""
from __future__ import annotations

import logging

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.user import User
from app.models.walker_profile import WalkerProfile
from app.services.walker_training_policy import get_enforcement, is_walker_trained

logger = logging.getLogger(__name__)

TRAINING_REQUIRED_CODE = "training_required"
TRAINING_REQUIRED_MESSAGE = "Conclua a Capacitação do passeador no app para aceitar passeios."


def enforce_training_completed(user: User, db: Session) -> None:
    enforcement = get_enforcement()
    if enforcement.effective_mode == "off":
        return
    profile = db.query(WalkerProfile).filter(WalkerProfile.user_id == user.id).first()
    if profile is None:
        # S2-3: sem perfil de passeador, o matching já exclui implicitamente (só ranqueia
        # WalkerProfile) — com trava EFETIVA (on), o gate bloqueia igual, em vez de deixar
        # passar; sem trava efetiva (off/warn), mantém o comportamento de sempre (passa —
        # quem não tem perfil já é barrado pelos checks de papel da própria rota).
        if enforcement.blocking:
            raise HTTPException(
                status_code=403,
                detail={
                    "code": TRAINING_REQUIRED_CODE,
                    "message": TRAINING_REQUIRED_MESSAGE,
                    "required_version": enforcement.required_version,
                },
            )
        return
    if is_walker_trained(profile, enforcement.required_version):
        return
    if not enforcement.blocking:
        logger.info("training_gate_warn user_id=%s reason=%s", user.id, enforcement.reason)
        return
    raise HTTPException(
        status_code=403,
        detail={
            "code": TRAINING_REQUIRED_CODE,
            "message": TRAINING_REQUIRED_MESSAGE,
            "required_version": enforcement.required_version,
        },
    )
