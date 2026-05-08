from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel


ComplaintSource = Literal["tutor", "walker", "admin", "system"]
ComplaintTargetType = Literal["walker", "tutor", "pet", "walk", "address", "service", "payment", "app"]
ComplaintSeverity = Literal["baixa", "media", "alta", "critica"]
ComplaintStatus = Literal["aberta", "em_analise", "aguardando_evidencia", "decidida", "resolvida", "rejeitada"]


class ComplaintEvidenceCreate(BaseModel):
    evidence_type: str = Field(default="note", max_length=40)
    url: str = ""
    description: str = Field(default="", max_length=800)


class ComplaintCreate(BaseModel):
    source: ComplaintSource
    target_type: ComplaintTargetType
    target_user_id: str | None = None
    target_pet_id: str | None = None
    walk_id: str | None = None
    category: str = Field(min_length=3, max_length=80)
    title: str = Field(default="", max_length=120)
    description: str = Field(min_length=10, max_length=2500)
    evidences: list[ComplaintEvidenceCreate] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ComplaintAdminUpdate(BaseModel):
    status: ComplaintStatus | None = None
    severity: ComplaintSeverity | None = None
    internal_note: str | None = Field(default=None, max_length=2000)


class ComplaintDecisionReview(BaseModel):
    decision_type: str = Field(min_length=3, max_length=80)
    decision_status: Literal["approved", "rejected", "applied"] = "approved"
    reason: str = Field(min_length=10, max_length=2000)


class ComplaintEvidenceResponse(ORMModel):
    id: str
    complaint_id: str
    evidence_type: str
    url: str
    description: str
    created_by_id: str
    created_at: datetime


class ComplaintDecisionResponse(ORMModel):
    id: str
    complaint_id: str
    decision_type: str
    decision_status: str
    severity_snapshot: str
    reason: str
    created_by: str
    created_at: datetime
    reviewed_by_admin_id: str | None = None
    reviewed_at: datetime | None = None


class ComplaintStatusHistoryResponse(ORMModel):
    id: str
    complaint_id: str
    from_status: str
    to_status: str
    note: str
    actor_id: str | None = None
    actor_role: str
    created_at: datetime


class ComplaintResponse(ORMModel):
    id: str
    source: str
    author_id: str
    author_role: str
    target_type: str
    target_user_id: str | None = None
    target_pet_id: str | None = None
    walk_id: str | None = None
    category: str
    severity: str
    status: str
    title: str
    description: str
    risk_score: float
    requires_manual_review: bool
    recurrence_count: int
    created_at: datetime
    updated_at: datetime
    resolved_at: datetime | None = None
    evidences: list[ComplaintEvidenceResponse] = []
    decisions: list[ComplaintDecisionResponse] = []
    history: list[ComplaintStatusHistoryResponse] = []


class ComplaintListResponse(BaseModel):
    items: list[ComplaintResponse]
    total: int


class RiskScoreResponse(ORMModel):
    id: str
    subject_type: str
    subject_id: str
    score: float
    severity: str
    complaints_count: int
    critical_count: int
    shared_walk_blocked: bool
    updated_at: datetime
