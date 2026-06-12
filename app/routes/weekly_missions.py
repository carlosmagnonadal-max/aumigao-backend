from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from fastapi import HTTPException

from app.core.database import get_db
from app.dependencies.auth import get_current_user, require_admin
from app.dependencies.rbac import require_permission
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.weekly_mission import (
    AdminWalkerWeeklyMissionsResponse,
    WeeklyMissionListResponse,
    WeeklyMissionSummaryResponse,
)
from app.services.tenant_plan_service import tenant_feature_enabled
from app.services.weekly_mission_service import (
    ensure_approved_walker,
    get_admin_walker_weekly_missions,
    get_walker_mission_summary,
    get_walker_weekly_missions,
    refresh_weekly_missions,
)


def _assert_weekly_missions_feature(user: User, db) -> None:
    tenant_id = user.tenant_id
    if not tenant_id:
        return
    tenant = db.get(Tenant, tenant_id)
    if tenant and not tenant_feature_enabled(tenant, db, "weekly_missions"):
        raise HTTPException(status_code=403, detail="Missões semanais não estão habilitadas para este tenant.")

walker_router = APIRouter(prefix="/walker/me/weekly-missions", tags=["walker-weekly-missions"])
api_walker_router = APIRouter(prefix="/api/walker/me/weekly-missions", tags=["walker-weekly-missions"])
admin_router = APIRouter(prefix="/admin/walkers", tags=["admin-weekly-missions"], dependencies=[Depends(require_permission("missions.read"))])
api_admin_router = APIRouter(prefix="/api/admin/walkers", tags=["admin-weekly-missions"], dependencies=[Depends(require_permission("missions.read"))])


@walker_router.get("", response_model=WeeklyMissionListResponse)
@api_walker_router.get("", response_model=WeeklyMissionListResponse)
def my_weekly_missions(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _assert_weekly_missions_feature(user, db)
    ensure_approved_walker(user, db)
    return get_walker_weekly_missions(user.id, db)


@walker_router.get("/summary", response_model=WeeklyMissionSummaryResponse)
@api_walker_router.get("/summary", response_model=WeeklyMissionSummaryResponse)
def my_weekly_mission_summary(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _assert_weekly_missions_feature(user, db)
    ensure_approved_walker(user, db)
    return get_walker_mission_summary(user.id, db)


@walker_router.post("/refresh", response_model=WeeklyMissionListResponse)
@api_walker_router.post("/refresh", response_model=WeeklyMissionListResponse)
def refresh_my_weekly_missions(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _assert_weekly_missions_feature(user, db)
    ensure_approved_walker(user, db)
    refresh_weekly_missions(user.id, db)
    return get_walker_weekly_missions(user.id, db)


@admin_router.get("/{walker_id}/weekly-missions", response_model=AdminWalkerWeeklyMissionsResponse)
@api_admin_router.get("/{walker_id}/weekly-missions", response_model=AdminWalkerWeeklyMissionsResponse)
def admin_walker_weekly_missions(walker_id: str, db: Session = Depends(get_db)):
    return get_admin_walker_weekly_missions(walker_id, db)
