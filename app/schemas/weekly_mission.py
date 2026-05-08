from datetime import datetime

from pydantic import BaseModel

from app.schemas.common import ORMModel

MISSION_STATUSES = {"not_started", "in_progress", "completed", "expired"}
REWARD_STATUSES = {"none", "future_benefit", "eligible", "granted", "cancelled"}


class WeeklyMissionResponse(ORMModel):
    id: str
    walker_id: str
    mission_type: str
    title: str
    description: str
    metric_key: str
    target_value: float
    current_value: float
    progress_percentage: float
    status: str
    week_start: datetime
    week_end: datetime
    reward_status: str
    reward_description: str | None = None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None
    expired_at: datetime | None = None
    motivational_message: str | None = None


class WeeklyMissionListResponse(BaseModel):
    week_start: datetime
    week_end: datetime
    missions: list[WeeklyMissionResponse]


class WeeklyMissionSummaryResponse(BaseModel):
    total_missions: int
    completed_missions: int
    in_progress_missions: int
    expired_missions: int
    week_start: datetime
    week_end: datetime
    motivational_message: str
    progress_percentage: float


class AdminWalkerWeeklyMissionsResponse(WeeklyMissionListResponse):
    walker_id: str
    walker_name: str
    status: str | None = None
    history: list[WeeklyMissionResponse] = []
