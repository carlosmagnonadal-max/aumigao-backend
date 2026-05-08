from datetime import datetime

from pydantic import BaseModel, Field


class ReputationScores(BaseModel):
    rating_score: float
    experience_score: float
    behavior_score: float
    consistency_score: float | None = None
    recent_rating_score: float | None = None
    risk_penalty: float
    hybrid_reputation_score: float
    risk_level: str


class IncentiveResponse(BaseModel):
    id: str
    walker_id: str
    incentive_type: str
    title: str
    description: str
    source: str
    status: str
    visibility_effect: str
    created_at: datetime
    updated_at: datetime | None = None
    expires_at: datetime | None = None
    granted_at: datetime | None = None
    revoked_at: datetime | None = None
    admin_notes: str | None = None


class RecoveryPlanResponse(BaseModel):
    id: str
    walker_id: str
    risk_level_at_start: str
    status: str
    reason: str
    recommended_actions: list[str] = Field(default_factory=list)
    started_at: datetime
    ends_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime | None = None


class MonitoringAlertResponse(BaseModel):
    id: str
    walker_id: str
    alert_type: str
    severity: str
    title: str
    description: str
    status: str
    source: str
    created_at: datetime
    resolved_at: datetime | None = None
    reviewed_by_admin_id: str | None = None
    admin_notes: str | None = None


class TipIntegrityFlagResponse(BaseModel):
    id: str
    walker_id: str
    tutor_id: str | None = None
    walk_id: str | None = None
    tip_amount: float
    flag_type: str
    severity: str
    status: str
    notes: str | None = None
    created_at: datetime
    reviewed_at: datetime | None = None


class WalkerReputationHealthResponse(BaseModel):
    rating_average: float
    reviews_count: int
    total_walks: int
    level: str
    hybrid_reputation_score: float
    risk_level: str
    active_incentives: list[IncentiveResponse]
    active_recovery_plan: RecoveryPlanResponse | None = None
    recommendations: list[str]
    motivational_message: str
    score_breakdown: ReputationScores
    tip_policy: str


class IncentiveListResponse(BaseModel):
    items: list[IncentiveResponse]
    total: int


class AdminWalkerQualityItem(BaseModel):
    walker_id: str
    name: str
    status: str | None = None
    rating_average: float
    reviews_count: int
    total_walks: int
    level: str
    hybrid_reputation_score: float
    risk_level: str
    open_alerts_count: int
    active_incentives_count: int
    active_recovery_plan: bool
    tip_flags_count: int
    cancellation_rate: float | None = None


class AdminWalkerQualityListResponse(BaseModel):
    items: list[AdminWalkerQualityItem]
    total: int


class AdminWalkerQualityDetailResponse(BaseModel):
    walker: AdminWalkerQualityItem
    score_breakdown: ReputationScores
    snapshots: list[ReputationScores]
    reviews: list[dict]
    alerts: list[MonitoringAlertResponse]
    recovery_plan: RecoveryPlanResponse | None = None
    incentives: list[IncentiveResponse]
    tip_integrity_flags: list[TipIntegrityFlagResponse]
    recommendations: list[str]
    tip_policy: str


class RecoveryPlanCreate(BaseModel):
    reason: str | None = None
    recommended_actions: list[str] | None = None
    ends_at: datetime | None = None


class RecoveryPlanUpdate(BaseModel):
    status: str | None = None
    admin_notes: str | None = None


class MonitoringAlertUpdate(BaseModel):
    status: str
    admin_notes: str | None = None


class TipIntegrityFlagUpdate(BaseModel):
    status: str
    notes: str | None = None


class IncentiveCreate(BaseModel):
    incentive_type: str = "recognition"
    title: str
    description: str | None = None
    source: str = "admin"
    visibility_effect: str = "none"
    expires_at: datetime | None = None
    admin_notes: str | None = None


class IncentiveUpdate(BaseModel):
    status: str | None = None
    admin_notes: str | None = None
