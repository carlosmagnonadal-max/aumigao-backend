"""Schemas de Incentivos (regras configuraveis por tenant + concessoes).

Incentivos — spec 2026-06-10. Monetario apenas REGISTRA amount; payout e follow-up.
"""
from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel


class IncentiveRuleResponse(ORMModel):
    id: str
    tenant_id: str
    key: str
    title: str
    description: str
    trigger_type: str
    threshold: float
    reward_type: str
    reward_value: float
    visibility_effect: str
    active: bool
    created_at: datetime
    updated_at: datetime | None = None


class IncentiveRuleCreate(BaseModel):
    key: str = Field(min_length=1)
    title: str = Field(min_length=1)
    description: str = ""
    # rating | completed_missions | hybrid_score | completed_walks
    trigger_type: str
    threshold: float = Field(default=0, ge=0)
    # recognition | visibility | monetary
    reward_type: str = "recognition"
    reward_value: float = Field(default=0, ge=0)
    visibility_effect: str = "none"
    active: bool = True


class IncentiveRuleUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1)
    description: str | None = None
    trigger_type: str | None = None
    threshold: float | None = Field(default=None, ge=0)
    reward_type: str | None = None
    reward_value: float | None = Field(default=None, ge=0)
    visibility_effect: str | None = None
    active: bool | None = None


# --------------------------------------------------------------------------- #
# Concessoes (WalkerIncentive) — admin grant manual / revoke / list
# --------------------------------------------------------------------------- #
class IncentiveGrantRequest(BaseModel):
    incentive_type: str = "recognition"
    title: str = Field(min_length=1)
    description: str | None = None
    source: str = "admin"
    visibility_effect: str = "none"
    reward_type: str = "recognition"
    amount: float = Field(default=0, ge=0)
    expires_at: datetime | None = None
    admin_notes: str | None = None


class IncentiveRevokeRequest(BaseModel):
    admin_notes: str | None = None


class GrantedIncentiveResponse(BaseModel):
    id: str
    walker_id: str
    incentive_type: str
    title: str
    description: str
    source: str
    reward_type: str
    amount: float
    status: str
    visibility_effect: str
    created_at: datetime
    updated_at: datetime | None = None
    expires_at: datetime | None = None
    granted_at: datetime | None = None
    revoked_at: datetime | None = None
    admin_notes: str | None = None


class GrantedIncentiveListResponse(BaseModel):
    items: list[GrantedIncentiveResponse]
    total: int
