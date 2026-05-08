from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel


WALKER_REFERRAL_STATUSES = {
    "pending",
    "invited",
    "registered",
    "under_review",
    "approved",
    "rejected",
    "converted",
    "cancelled",
}

WALKER_REWARD_STATUSES = {"not_eligible", "pending", "eligible", "paid", "cancelled"}

WALKER_PERFORMANCE_STATUSES = {"neutral", "good", "excellent", "warning", "bad"}


class WalkerReferralCreate(BaseModel):
    referred_name: str = Field(min_length=2)
    referred_phone: str = Field(min_length=8)
    city: str = Field(min_length=2)
    neighborhood: str = Field(min_length=2)
    notes: str | None = None


class WalkerReferralValidateCode(BaseModel):
    referral_code: str


class WalkerReferralLinkUser(BaseModel):
    referral_code: str | None = None


class AdminWalkerReferralStatusUpdate(BaseModel):
    status: str
    rejection_reason: str | None = None
    reward_amount: float | None = None
    completed_walks_count: int | None = None
    average_rating: float | None = None
    performance_status: str | None = None


class WalkerReferralResponse(ORMModel):
    id: str
    referrer_user_id: str
    referred_user_id: str | None = None
    referred_name: str
    referred_phone: str
    city: str
    neighborhood: str
    notes: str | None = None
    referral_code: str
    invite_link: str | None = None
    status: str
    reward_status: str
    reward_amount: float | None = None
    completed_walks_count: int
    average_rating: float | None = None
    performance_status: str | None = None
    rejection_reason: str | None = None
    created_at: datetime
    updated_at: datetime
    approved_at: datetime | None = None
    rejected_at: datetime | None = None
    converted_at: datetime | None = None


class AdminWalkerReferralResponse(WalkerReferralResponse):
    referrer_name: str = ""
    referrer_role: str = ""
    referred_user_name: str | None = None


class WalkerReferralListResponse(BaseModel):
    items: list[WalkerReferralResponse]
    total: int


class AdminWalkerReferralListResponse(BaseModel):
    items: list[AdminWalkerReferralResponse]
    total: int


class WalkerReferralSummary(BaseModel):
    total: int
    pending: int
    approved: int
    converted: int
    eligible_reward: float
    paid_reward: float
