from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.dependencies.auth import get_current_user, require_admin
from app.dependencies.rbac import require_permission
from app.models.user import User
from app.models.walker_incentive import WalkerIncentive
from app.models.walker_monitoring_alert import WalkerMonitoringAlert
from app.models.walker_recovery_plan import WalkerRecoveryPlan
from app.schemas.walker_quality import (
    AdminWalkerQualityDetailResponse,
    AdminWalkerQualityListResponse,
    IncentiveCreate,
    IncentiveListResponse,
    IncentiveResponse,
    IncentiveUpdate,
    MonitoringAlertResponse,
    MonitoringAlertUpdate,
    RecoveryPlanCreate,
    RecoveryPlanResponse,
    RecoveryPlanUpdate,
    TipIntegrityFlagResponse,
    TipIntegrityFlagUpdate,
    WalkerReputationHealthResponse,
)
from app.services.incentive_engine_service import grant_incentive, incentive_payload, list_incentives, revoke_incentive
from app.services.monitoring_service import alert_payload, update_alert
from app.services.recovery_service import get_or_create_recovery_plan, recovery_payload, update_recovery_plan_status
from app.services.reputation_service import create_reputation_snapshot
from app.services.tip_integrity_service import review_tip_flag, tip_flag_payload
from app.services.walker_quality_service import get_quality_dashboard, get_walker_quality_detail, get_walker_reputation_health

walker_router = APIRouter(prefix="/walker/me", tags=["walker-quality"])
api_walker_router = APIRouter(prefix="/api/walker/me", tags=["walker-quality"])
admin_router = APIRouter(prefix="/admin", tags=["admin-walker-quality"], dependencies=[Depends(require_permission("quality.read"))])
api_admin_router = APIRouter(prefix="/api/admin", tags=["admin-walker-quality"], dependencies=[Depends(require_permission("quality.read"))])


@walker_router.get("/reputation-health", response_model=WalkerReputationHealthResponse)
@api_walker_router.get("/reputation-health", response_model=WalkerReputationHealthResponse)
def my_reputation_health(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return get_walker_reputation_health(user, db)


@walker_router.get("/incentives", response_model=IncentiveListResponse)
@api_walker_router.get("/incentives", response_model=IncentiveListResponse)
def my_incentives(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    incentives = list_incentives(user.id, db)
    return {"items": [incentive_payload(incentive) for incentive in incentives], "total": len(incentives)}


@walker_router.get("/recovery-plan", response_model=RecoveryPlanResponse | None)
@api_walker_router.get("/recovery-plan", response_model=RecoveryPlanResponse | None)
def my_recovery_plan(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    plan = get_or_create_recovery_plan(user.id, db)
    return recovery_payload(plan) if plan else None


@admin_router.get("/walker-quality", response_model=AdminWalkerQualityListResponse)
@api_admin_router.get("/walker-quality", response_model=AdminWalkerQualityListResponse)
def admin_walker_quality(
    risk_level: str | None = Query(None),
    status: str | None = Query(None),
    has_open_alerts: bool | None = Query(None),
    has_recovery_plan: bool | None = Query(None),
    has_tip_flags: bool | None = Query(None),
    db: Session = Depends(get_db),
):
    return get_quality_dashboard(db, risk_level, status, has_open_alerts, has_recovery_plan, has_tip_flags)


@admin_router.get("/walkers/{walker_id}/quality", response_model=AdminWalkerQualityDetailResponse)
@api_admin_router.get("/walkers/{walker_id}/quality", response_model=AdminWalkerQualityDetailResponse)
def admin_walker_quality_detail(walker_id: str, db: Session = Depends(get_db)):
    return get_walker_quality_detail(walker_id, db)


@admin_router.post("/walkers/{walker_id}/recalculate-reputation")
@api_admin_router.post("/walkers/{walker_id}/recalculate-reputation")
def admin_recalculate_reputation(walker_id: str, db: Session = Depends(get_db)):
    snapshot = create_reputation_snapshot(walker_id, db)
    return {"ok": True, "walker_id": walker_id, "hybrid_reputation_score": snapshot.hybrid_reputation_score, "risk_level": snapshot.risk_level}


@admin_router.post("/walkers/{walker_id}/recovery-plan", response_model=RecoveryPlanResponse)
@api_admin_router.post("/walkers/{walker_id}/recovery-plan", response_model=RecoveryPlanResponse)
def admin_create_recovery_plan(walker_id: str, payload: RecoveryPlanCreate, db: Session = Depends(get_db)):
    plan = get_or_create_recovery_plan(walker_id, db, reason=payload.reason, actions=payload.recommended_actions, force=True)
    if payload.ends_at and plan:
        plan.ends_at = payload.ends_at
        db.commit()
        db.refresh(plan)
    return recovery_payload(plan)


@admin_router.patch("/recovery-plans/{plan_id}", response_model=RecoveryPlanResponse)
@api_admin_router.patch("/recovery-plans/{plan_id}", response_model=RecoveryPlanResponse)
def admin_update_recovery_plan(plan_id: str, payload: RecoveryPlanUpdate, db: Session = Depends(get_db)):
    plan = update_recovery_plan_status(plan_id, payload.status or "active", db)
    return recovery_payload(plan)


@admin_router.get("/monitoring-alerts")
@api_admin_router.get("/monitoring-alerts")
def admin_monitoring_alerts(status: str | None = Query(None), db: Session = Depends(get_db)):
    query = db.query(WalkerMonitoringAlert)
    if status and status != "all":
        query = query.filter(WalkerMonitoringAlert.status == status)
    alerts = query.order_by(WalkerMonitoringAlert.created_at.desc()).all()
    return {"items": [alert_payload(alert) for alert in alerts], "total": len(alerts)}


@admin_router.patch("/monitoring-alerts/{alert_id}", response_model=MonitoringAlertResponse)
@api_admin_router.patch("/monitoring-alerts/{alert_id}", response_model=MonitoringAlertResponse)
def admin_update_monitoring_alert(alert_id: str, payload: MonitoringAlertUpdate, admin: User = Depends(require_permission("alerts.resolve")), db: Session = Depends(get_db)):
    return alert_payload(update_alert(alert_id, payload.status, payload.admin_notes, admin.id, db))


@admin_router.get("/tip-integrity-flags")
@api_admin_router.get("/tip-integrity-flags")
def admin_tip_integrity_flags(status: str | None = Query(None), db: Session = Depends(get_db)):
    from app.models.tip_integrity_flag import TipIntegrityFlag

    query = db.query(TipIntegrityFlag)
    if status and status != "all":
        query = query.filter(TipIntegrityFlag.status == status)
    flags = query.order_by(TipIntegrityFlag.created_at.desc()).all()
    return {"items": [tip_flag_payload(flag) for flag in flags], "total": len(flags)}


@admin_router.patch("/tip-integrity-flags/{flag_id}", response_model=TipIntegrityFlagResponse)
@api_admin_router.patch("/tip-integrity-flags/{flag_id}", response_model=TipIntegrityFlagResponse)
def admin_update_tip_integrity_flag(flag_id: str, payload: TipIntegrityFlagUpdate, db: Session = Depends(get_db)):
    return tip_flag_payload(review_tip_flag(flag_id, payload.status, payload.notes, db))


@admin_router.post("/walkers/{walker_id}/incentives", response_model=IncentiveResponse)
@api_admin_router.post("/walkers/{walker_id}/incentives", response_model=IncentiveResponse)
def admin_grant_incentive(walker_id: str, payload: IncentiveCreate, db: Session = Depends(get_db)):
    incentive = grant_incentive(
        walker_id,
        payload.incentive_type,
        payload.title,
        payload.description or "",
        payload.source,
        db,
        visibility_effect=payload.visibility_effect,
        expires_at=payload.expires_at,
        admin_notes=payload.admin_notes,
    )
    return incentive_payload(incentive)


@admin_router.patch("/incentives/{incentive_id}", response_model=IncentiveResponse)
@api_admin_router.patch("/incentives/{incentive_id}", response_model=IncentiveResponse)
def admin_update_incentive(incentive_id: str, payload: IncentiveUpdate, db: Session = Depends(get_db)):
    if payload.status == "revoked":
        return incentive_payload(revoke_incentive(incentive_id, db, payload.admin_notes))
    incentive = db.get(WalkerIncentive, incentive_id)
    if not incentive:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Incentivo nao encontrado")
    if payload.status:
        incentive.status = payload.status
    if payload.admin_notes:
        incentive.admin_notes = payload.admin_notes
    db.commit()
    db.refresh(incentive)
    return incentive_payload(incentive)
