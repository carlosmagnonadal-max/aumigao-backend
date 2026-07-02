"""BG-reverificacao — expiracao periodica das certidoes de antecedentes.

Certidao e um retrato de um instante; para nao apodrecer, uma certidao `validated`
ha mais de N dias (default 365, env BACKGROUND_CERT_MAX_AGE_DAYS) vira `expired`.
Isso recalcula o status agregado do passeador (deixa de ser `verified` -> reabre a
pendencia e o gate do approve volta a valer) e notifica o passeador a reemitir — a
certidao e GRATIS, entao a mensagem diz isso.

- So age em tenants com a flag `background_checks` ON (sem flag = no-op).
  Como WalkerProfile e global (o tenant esta no User), resolvemos o tenant do
  passeador via user.tenant_id.
- Idempotente: rodar 2x no dia nao duplica notificacao — so notifica na TRANSICAO
  validated -> expired (uma certidao ja `expired` nao e reprocessada).

Disparo: endpoint interno POST /internal/background-reverification/sweep protegido
por INTERNAL_SWEEP_TOKEN (mesmo padrao do credit-expiry/sweep), chamado pelo Cloud
Scheduler. CRON e fail-closed normal (interno).
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.models.tenant import Tenant
from app.models.user import User
from app.models.walker_background_certificate import WalkerBackgroundCertificate
from app.models.walker_profile import WalkerProfile
from app.services.background_check_service import compute_background_status
from app.services.tenant_feature_runtime_service import is_tenant_feature_enabled

logger = logging.getLogger("aumigao.background_reverification_service")

_DEFAULT_MAX_AGE_DAYS = 365


def _max_age_days() -> int:
    """Idade maxima (dias) de uma certidao validada antes de expirar. Default 365."""
    raw = os.getenv("BACKGROUND_CERT_MAX_AGE_DAYS")
    try:
        value = int(raw) if raw is not None else _DEFAULT_MAX_AGE_DAYS
    except (TypeError, ValueError):
        value = _DEFAULT_MAX_AGE_DAYS
    return value if value > 0 else _DEFAULT_MAX_AGE_DAYS


def _notify_walker_reissue(db: Session, profile: WalkerProfile, tenant_id: str | None) -> None:
    """Notifica o passeador a reemitir a certidao vencida (best-effort).

    A certidao e GRATIS — a mensagem deixa isso claro para reduzir atrito.
    """
    try:
        from app.routes.notifications import NotificationCreate, _create_notification

        _create_notification(db, NotificationCreate(
            user_id=profile.user_id,
            user_role="walker",
            tenant_id=tenant_id,
            title="🔄 Sua certidao de antecedentes venceu",
            message=(
                "Sua certidao de antecedentes passou da validade e precisa ser "
                "reemitida para manter seu cadastro em dia. A emissao e GRATUITA nos "
                "orgaos oficiais — envie a nova certidao pelo app."
            ),
            type="background_reverification",
            related_entity_type="walker",
            related_entity_id=profile.user_id,
            metadata={"walker_profile_id": profile.id},
        ))
    except Exception:
        logger.exception(
            "reverificacao: falha best-effort ao notificar passeador user_id=%s",
            getattr(profile, "user_id", None),
        )


def _process_profile(db: Session, profile: WalkerProfile, tenant_id: str | None, cutoff: datetime) -> int:
    """Expira certidoes validadas vencidas de UM passeador e recalcula o agregado.

    Retorna quantas certidoes foram expiradas (0 se nada mudou). So notifica se
    houve ao menos uma transicao validated -> expired.
    """
    certificates = (
        db.query(WalkerBackgroundCertificate)
        .filter(WalkerBackgroundCertificate.walker_profile_id == profile.id)
        .all()
    )
    expired = 0
    now = datetime.utcnow()
    for cert in certificates:
        if cert.status != "validated":
            continue
        # Sem validated_at nao da para julgar idade — deixa como esta (conservador).
        if cert.validated_at is None:
            continue
        if cert.validated_at <= cutoff:
            cert.status = "expired"
            cert.updated_at = now
            expired += 1

    if expired:
        # Recalcula o agregado (verified -> submitted/partial/none) e reabre o gate.
        compute_background_status(profile, certificates)
        _notify_walker_reissue(db, profile, tenant_id)

    return expired


def sweep_stale_certificates(db: Session) -> dict:
    """Varre certidoes validadas vencidas nos tenants com `background_checks` ON.

    Idempotente. Retorna dict com contagens para observabilidade.
    """
    cutoff = datetime.utcnow() - timedelta(days=_max_age_days())
    total_expired = 0
    profiles_touched = 0

    tenants = db.query(Tenant).all()
    for tenant in tenants:
        if not is_tenant_feature_enabled(db, "background_checks", tenant_id=tenant.id):
            continue
        # WalkerProfile e global — resolvemos os passeadores do tenant via user.tenant_id.
        user_ids = [
            row[0]
            for row in db.query(User.id).filter(User.tenant_id == tenant.id).all()
        ]
        if not user_ids:
            continue
        profiles = (
            db.query(WalkerProfile)
            .filter(WalkerProfile.user_id.in_(user_ids))
            .all()
        )
        for profile in profiles:
            n = _process_profile(db, profile, tenant.id, cutoff)
            if n:
                profiles_touched += 1
                total_expired += n

    if total_expired:
        db.commit()

    logger.info(
        "reverificacao: expired=%d profiles_touched=%d",
        total_expired, profiles_touched,
    )
    return {"expired": total_expired, "profiles_touched": profiles_touched}
