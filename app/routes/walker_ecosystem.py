"""Endpoints do ecossistema do passeador (CR, gorjetas, badges, gamificação, notificações,
kit de certificação, evolução integrada e agregador de ecossistema).

Padrão dual-router espelhando walker_quality.py:
  - walker_router   → prefix="/walker/me"   (sem /api)
  - api_walker_router → prefix="/api/walker/me"

Todos os endpoints exigem passeador autenticado (get_current_user).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.dependencies.auth import get_current_user
from app.models.user import User
from app.models.walk_tip import WalkTip
from app.models.walker_incentive import WalkerIncentive
from app.models.walker_kit_submission import WalkerKitSubmission
from app.models.walker_monitoring_alert import WalkerMonitoringAlert
from app.services.incentive_engine_service import incentive_payload, list_incentives
from app.services.monitoring_service import alert_payload, open_alerts
from app.services.recovery_service import get_or_create_recovery_plan, recovery_payload
from app.services.reputation_service import calculate_hybrid_reputation_score, reputation_summary
from app.services import walker_cr_service
from app.services import walker_gamification_service
from app.services import walker_smart_notification_service

walker_router = APIRouter(prefix="/walker/me", tags=["walker-ecosystem"])
api_walker_router = APIRouter(prefix="/api/walker/me", tags=["walker-ecosystem"])

# ---------------------------------------------------------------------------
# Política legível de CR (exposta no shape do wallet para o frontend).
# ---------------------------------------------------------------------------
_CR_SOURCE_POLICY = (
    "CR e concedido pela plataforma por performance; "
    "nao e comprado pelo passeador."
)

# ---------------------------------------------------------------------------
# Helpers de serialização
# ---------------------------------------------------------------------------

def _wallet_payload(wallet) -> dict:
    return {
        "id": wallet.id,
        "walker_id": wallet.walker_user_id,
        "balance": wallet.balance,
        "lifetime_earned": wallet.lifetime_earned,
        "lifetime_spent": wallet.lifetime_spent,
        "source_policy": _CR_SOURCE_POLICY,
        "created_at": wallet.created_at.isoformat() if wallet.created_at else None,
        "updated_at": wallet.updated_at.isoformat() if wallet.updated_at else None,
    }


def _tx_payload(tx) -> dict:
    return {
        "id": tx.id,
        "walker_id": tx.walker_user_id,
        "amount": tx.amount,
        "transaction_type": tx.tx_type,   # frontend reads transaction_type
        "source": tx.source,
        "description": tx.description,
        "related_entity_type": tx.related_entity_type,
        "related_entity_id": tx.related_entity_id,
        "created_at": tx.created_at.isoformat() if tx.created_at else None,
    }


def _tip_payload(tip: WalkTip) -> dict:
    return {
        "id": tip.id,
        "walk_id": tip.walk_id,
        "tutor_id": tip.tutor_id,
        "walker_id": tip.walker_id,
        "amount": float(tip.amount),
        "status": tip.status,
        "payment_reference": tip.provider_payment_id,
        "created_at": tip.created_at.isoformat() if tip.created_at else None,
    }


def _badge_payload(incentive: WalkerIncentive) -> dict:
    """Maps WalkerIncentive → badge shape expected by demoWalkerBadges."""
    return {
        "id": incentive.id,
        "walker_id": incentive.walker_id,
        "badge_type": incentive.incentive_type,   # e.g. "badge", "premium_badge"
        "title": incentive.title,
        "description": incentive.description,
        "status": incentive.status,
        "source": incentive.source,
        "granted_at": incentive.granted_at.isoformat() if incentive.granted_at else None,
        "expires_at": incentive.expires_at.isoformat() if incentive.expires_at else None,
        "revoked_at": incentive.revoked_at.isoformat() if incentive.revoked_at else None,
        "revocation_reason": None,
        "criteria_snapshot_json": None,
        "admin_notes": incentive.admin_notes,
        "created_at": incentive.created_at.isoformat() if incentive.created_at else None,
        "updated_at": incentive.updated_at.isoformat() if incentive.updated_at else None,
    }


def _premium_badge_payload(incentive: WalkerIncentive) -> dict:
    """Maps WalkerIncentive → demoWalkerPremiumBadge shape."""
    return {
        "id": incentive.id,
        "walker_id": incentive.walker_id,
        "status": incentive.status,
        "granted_at": incentive.granted_at.isoformat() if incentive.granted_at else None,
        "expires_at": incentive.expires_at.isoformat() if incentive.expires_at else None,
        "revoked_at": incentive.revoked_at.isoformat() if incentive.revoked_at else None,
        "revocation_reason": None,
        "label": incentive.title,
        "criteria_snapshot_json": None,
        "created_at": incentive.created_at.isoformat() if incentive.created_at else None,
        "updated_at": incentive.updated_at.isoformat() if incentive.updated_at else None,
    }


def _gamification_event_payload(event) -> dict:
    return {
        "id": event.id,
        "walker_id": event.walker_user_id,
        "event_type": event.event_type,
        "title": event.title,
        "description": event.description,
        # frontend reads both cr_amount AND points_value (some screens use one, some the other)
        "cr_amount": event.cr_amount,
        "points_value": event.cr_amount,
        "related_entity_type": event.related_entity_type,
        "related_entity_id": event.related_entity_id,
        "created_at": event.created_at.isoformat() if event.created_at else None,
    }


def _smart_notification_payload(notif) -> dict:
    return {
        "id": notif.id,
        "walker_id": notif.walker_user_id,
        "notification_type": notif.notification_type,
        "title": notif.title,
        "message": notif.message,
        "priority": notif.priority,
        "trigger_source": notif.trigger_source,
        "read_at": notif.read_at.isoformat() if notif.read_at else None,
        "sent_at": notif.sent_at.isoformat() if notif.sent_at else None,
        "created_at": notif.created_at.isoformat() if notif.created_at else None,
        "expires_at": notif.expires_at.isoformat() if notif.expires_at else None,
    }


def _kit_certification_payload(kit_row: WalkerKitSubmission | None) -> dict | None:
    """Map WalkerKitSubmission → demoWalkerCertificationKit shape.

    Returns None when the walker has no kit submission at all.
    Returns a dict with status='active' if approved, else 'pending' or 'none'.
    """
    if kit_row is None:
        return None
    status = "active" if kit_row.audit_status == "approved" else (
        "pending" if kit_row.audit_status in ("pending_review",) else "none"
    )
    return {
        "id": kit_row.id if hasattr(kit_row, "id") else None,
        "walker_id": kit_row.walker_user_id,
        "status": status,
        "kit_type": "standard",
        "issued_at": None,
        "delivered_at": None,
        "expires_at": None,
        "revoked_at": None,
        "revocation_reason": None,
        "admin_notes": kit_row.audit_note,
        "label": "Passeador certificado" if status == "active" else "Kit em analise",
        "created_at": None,
        "updated_at": kit_row.updated_at.isoformat() if kit_row.updated_at else None,
    }


def _get_walker_level(user: User, db: Session) -> dict:
    """Returns the level object used in ecosystem / evolution endpoints.

    Mirrors how /walker/me/level works: calls _goals_evolution_payload from
    walker.py route. Since we can't import the private function, we replicate the
    minimal logic: reputation_summary → walker_trust_service → level structure.
    """
    from app.services.walker_trust_service import compute_walker_trust
    from app.services.reputation_service import completed_walks_count, walker_level

    summary = reputation_service_summary_cached(user.id, db)
    trust = compute_walker_trust(db, user.id)
    total_walks = summary["total_walks"]
    rating_avg = summary["rating_average"]
    reviews_count = summary["reviews_count"]

    # compute simple progress + next_level string
    # Use the official level from trust service
    official_level = trust["level"]

    return {
        "current_level": official_level.lower().replace(" ", "_"),
        "progress_percentage": _compute_progress(total_walks, rating_avg, official_level),
        "next_level": _next_level_key(official_level),
    }


def reputation_service_summary_cached(walker_id: str, db: Session) -> dict:
    """Thin wrapper so we don't import the private route function."""
    from app.services.reputation_service import reputation_summary as _rep_summary
    return _rep_summary(walker_id, db)


def _compute_progress(total_walks: int, rating_avg: float, current_level: str) -> int:
    """Rough progress towards the next level (0–100)."""
    from app.constants import (
        LEVEL_PRATA_MIN_WALKS, LEVEL_OURO_MIN_WALKS, LEVEL_DIAMANTE_MIN_WALKS,
    )
    thresholds = {
        "Bronze": (0, LEVEL_PRATA_MIN_WALKS),
        "Prata": (LEVEL_PRATA_MIN_WALKS, LEVEL_OURO_MIN_WALKS),
        "Ouro": (LEVEL_OURO_MIN_WALKS, LEVEL_DIAMANTE_MIN_WALKS),
        "Diamante": (LEVEL_DIAMANTE_MIN_WALKS, LEVEL_DIAMANTE_MIN_WALKS),
    }
    low, high = thresholds.get(current_level, (0, 10))
    if high == low:
        return 100
    return min(100, round((total_walks - low) / max(1, high - low) * 100))


def _next_level_key(current_level: str) -> str | None:
    order = ["Bronze", "Prata", "Ouro", "Diamante"]
    try:
        idx = order.index(current_level)
        return order[idx + 1].lower() if idx + 1 < len(order) else None
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# 1. GET /walker/me/cr
# ---------------------------------------------------------------------------

@walker_router.get("/cr")
@api_walker_router.get("/cr")
def my_cr(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Carteira de CR + histórico de transações do passeador autenticado."""
    wallet = walker_cr_service.get_or_create_wallet(db, user.id)
    db.commit()  # commit da criação de wallet se necessário
    transactions = walker_cr_service.list_transactions(db, user.id)
    return {
        "wallet": _wallet_payload(wallet),
        "transactions": [_tx_payload(tx) for tx in transactions],
    }


# ---------------------------------------------------------------------------
# 2. GET /walker/me/tips
# ---------------------------------------------------------------------------

_TIP_POLICY = (
    "Gorjetas aparecem no financeiro e nao entram em reputacao, "
    "ranking, matching, nivel, selo ou boost."
)


@walker_router.get("/tips")
@api_walker_router.get("/tips")
def my_tips(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Gorjetas recebidas pelo passeador."""
    tips = (
        db.query(WalkTip)
        .filter(WalkTip.walker_id == user.id)
        .order_by(WalkTip.created_at.desc())
        .all()
    )
    total_amount = round(sum(float(t.amount) for t in tips if t.status == "paid"), 2)
    return {
        "items": [_tip_payload(t) for t in tips],
        "total": len(tips),
        "total_amount": total_amount,
        "policy": _TIP_POLICY,
    }


# ---------------------------------------------------------------------------
# 3. GET /walker/me/badges
# ---------------------------------------------------------------------------

@walker_router.get("/badges")
@api_walker_router.get("/badges")
def my_badges(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Badges ativos do passeador (incentivos do tipo badge, active)."""
    badges = (
        db.query(WalkerIncentive)
        .filter(
            WalkerIncentive.walker_id == user.id,
            WalkerIncentive.incentive_type.contains("badge"),
            WalkerIncentive.status == "active",
        )
        .order_by(WalkerIncentive.created_at.desc())
        .all()
    )
    return {
        "items": [_badge_payload(b) for b in badges],
        "total": len(badges),
    }


# ---------------------------------------------------------------------------
# 4. GET /walker/me/premium-badge
# ---------------------------------------------------------------------------

@walker_router.get("/premium-badge")
@api_walker_router.get("/premium-badge")
def my_premium_badge(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Badge premium ativo mais recente do passeador, ou null."""
    badge = (
        db.query(WalkerIncentive)
        .filter(
            WalkerIncentive.walker_id == user.id,
            WalkerIncentive.incentive_type == "premium_badge",
            WalkerIncentive.status == "active",
        )
        .order_by(WalkerIncentive.created_at.desc())
        .first()
    )
    if badge is None:
        return None
    return _premium_badge_payload(badge)


# ---------------------------------------------------------------------------
# 5. GET /walker/me/gamification
# ---------------------------------------------------------------------------

@walker_router.get("/gamification")
@api_walker_router.get("/gamification")
def my_gamification(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Eventos de gamificação do passeador."""
    events = walker_gamification_service.list_events(db, user.id)
    return {
        "items": [_gamification_event_payload(e) for e in events],
        "total": len(events),
    }


# ---------------------------------------------------------------------------
# 6. GET /walker/me/smart-notifications
#    PATCH /walker/me/smart-notifications/{id}/read
# ---------------------------------------------------------------------------

@walker_router.get("/smart-notifications")
@api_walker_router.get("/smart-notifications")
def my_smart_notifications(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Notificações inteligentes do passeador."""
    notifications = walker_smart_notification_service.list_notifications(db, user.id)
    unread_count = walker_smart_notification_service.count_unread(db, user.id)
    return {
        "items": [_smart_notification_payload(n) for n in notifications],
        "total": len(notifications),
        "unread_count": unread_count,
    }


@walker_router.patch("/smart-notifications/{notification_id}/read")
@api_walker_router.patch("/smart-notifications/{notification_id}/read")
def mark_smart_notification_read(
    notification_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Marca uma notificação inteligente como lida."""
    notif = walker_smart_notification_service.mark_read(db, notification_id, user.id)
    if notif is None:
        raise HTTPException(status_code=404, detail="Notificacao nao encontrada.")
    return _smart_notification_payload(notif)


# ---------------------------------------------------------------------------
# 7. GET /walker/me/certification-kit
# ---------------------------------------------------------------------------

@walker_router.get("/certification-kit")
@api_walker_router.get("/certification-kit")
def my_certification_kit(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Kit de certificação físico do passeador."""
    kit_row = (
        db.query(WalkerKitSubmission)
        .filter(WalkerKitSubmission.walker_user_id == user.id)
        .first()
    )
    payload = _kit_certification_payload(kit_row)
    if payload is None:
        return {"status": "none", "label": "Kit nao enviado"}
    return payload


# ---------------------------------------------------------------------------
# 8. GET /walker/me/ecosystem  (agregador)
# ---------------------------------------------------------------------------

@walker_router.get("/ecosystem")
@api_walker_router.get("/ecosystem")
def my_ecosystem(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Agregador: reúne todos os dados do ecossistema do passeador em uma resposta."""
    # Wallet de CR
    wallet = walker_cr_service.get_or_create_wallet(db, user.id)
    db.commit()

    # Reputação + resumo
    summary = reputation_service_summary_cached(user.id, db)
    scores = calculate_hybrid_reputation_score(user.id, db)

    # Nível
    level_data = _get_walker_level(user, db)

    # Premium badge
    premium_badge_row = (
        db.query(WalkerIncentive)
        .filter(
            WalkerIncentive.walker_id == user.id,
            WalkerIncentive.incentive_type == "premium_badge",
            WalkerIncentive.status == "active",
        )
        .order_by(WalkerIncentive.created_at.desc())
        .first()
    )

    # Kit de certificação
    kit_row = (
        db.query(WalkerKitSubmission)
        .filter(WalkerKitSubmission.walker_user_id == user.id)
        .first()
    )

    # Incentivos
    incentives = list_incentives(user.id, db)

    # Alertas abertos
    alerts = open_alerts(user.id, db)

    # Notificações não lidas (últimas 20)
    notifications = walker_smart_notification_service.list_notifications(db, user.id, limit=20)
    unread_count = walker_smart_notification_service.count_unread(db, user.id)

    # Plano de recuperação
    recovery_plan = get_or_create_recovery_plan(user.id, db)

    # Recomendações
    from app.services.recovery_service import build_recommendations
    recommendations = build_recommendations(user.id, db)

    return {
        "walker_id": user.id,
        "level": level_data,
        "reputation": {
            "rating_average": summary["rating_average"],
            "reviews_count": summary["reviews_count"],
            "total_walks": summary["total_walks"],
            "hybrid_reputation_score": scores.get("hybrid_reputation_score"),
            "risk_level": scores.get("risk_level", "normal"),
            "tip_policy": (
                "Gorjetas ficam no financeiro e nao alteram reputacao, "
                "matching, nivel, boost ou prioridade no MVP."
            ),
        },
        "cr_wallet": _wallet_payload(wallet),
        "premium_badge": _premium_badge_payload(premium_badge_row) if premium_badge_row else None,
        "certification_kit": _kit_certification_payload(kit_row),
        "incentives": [incentive_payload(i) for i in incentives],
        "alerts": [alert_payload(a) for a in alerts],
        "recommendations": recommendations,
        "notifications": [_smart_notification_payload(n) for n in notifications],
        "unread_count": unread_count,
        "recovery_plan": recovery_payload(recovery_plan) if recovery_plan else None,
        "matching_impact": {
            "public_badges": [i.title for i in incentives if i.status == "active"],
            "boost_status": "available",
            "internal_note": (
                "Matching pode usar selo e nivel como sinais visuais/controlados. "
                "CR nao entra como reputacao."
            ),
        },
    }


# ---------------------------------------------------------------------------
# 9. GET /walker/me/evolution
# ---------------------------------------------------------------------------

@walker_router.get("/evolution")
@api_walker_router.get("/evolution")
def my_evolution(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Evolução integrada do passeador: nível, CR, badges, gamificação, recomendações."""
    wallet = walker_cr_service.get_or_create_wallet(db, user.id)
    db.commit()

    level_data = _get_walker_level(user, db)

    badges = (
        db.query(WalkerIncentive)
        .filter(
            WalkerIncentive.walker_id == user.id,
            WalkerIncentive.incentive_type.contains("badge"),
            WalkerIncentive.status == "active",
        )
        .order_by(WalkerIncentive.created_at.desc())
        .all()
    )

    events = walker_gamification_service.list_events(db, user.id, limit=20)

    from app.services.recovery_service import build_recommendations
    recommendations = build_recommendations(user.id, db)

    return {
        "walker_id": user.id,
        "level": level_data,
        "cr_balance": wallet.balance,
        "badges": [_badge_payload(b) for b in badges],
        "gamification_events": [_gamification_event_payload(e) for e in events],
        "recommendations": recommendations,
        "policy": (
            "Gorjeta, cupom, compra de kit e CR nao compram reputacao, "
            "ranking, nivel ou selo."
        ),
    }
