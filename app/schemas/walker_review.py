from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel
from app.schemas.walker_trust import WalkerTrustResponse


class WalkerReviewCreate(BaseModel):
    # Sec-P3: max_length defensivo no campo de texto livre.
    rating: int = Field(ge=1, le=5)
    comment: str | None = Field(None, max_length=2000)


class WalkerReviewFlagUpdate(BaseModel):
    is_flagged: bool
    admin_notes: str | None = None


class WalkerReviewResponse(ORMModel):
    id: str
    walk_id: str
    tutor_id: str
    walker_id: str
    rating: int
    comment: str | None = None
    punctuality_rating: int | None = None
    care_rating: int | None = None
    communication_rating: int | None = None
    is_flagged: bool = False
    admin_notes: str | None = None
    created_at: datetime
    updated_at: datetime


class PublicWalkerReview(BaseModel):
    id: str
    walk_id: str
    rating: int
    comment: str | None = None
    tutor_name: str
    created_at: datetime


class WalkerReviewListResponse(BaseModel):
    items: list[PublicWalkerReview]
    total: int


class WalkerReputationSummary(BaseModel):
    rating_average: float
    reviews_count: int
    total_walks: int
    level: str
    reputation_score: float | None = None
    acceptance_rate: float | None = None
    cancellation_rate: float | None = None


class PublicWalkerProfileResponse(WalkerReputationSummary):
    id: str
    name: str
    photo: str | None = None
    status: str | None = None
    recent_reviews: list[PublicWalkerReview]
    member_since: datetime | None = None
    bio: str | None = None
    city: str | None = None
    neighborhood: str | None = None
    walker_kit: dict | None = None
    empty_message: str | None = None
    trust: WalkerTrustResponse | None = None


class WalkerPerformanceResponse(WalkerReputationSummary):
    recent_reviews: list[PublicWalkerReview]
    motivational_message: str
    future_score_inputs: dict


class AdminWalkerReputationResponse(PublicWalkerProfileResponse):
    reviews: list[WalkerReviewResponse]
    flagged_reviews_count: int
    referral_performance: dict | None = None
    future_score_inputs: dict


class AdminWalkerReputationListItem(WalkerReputationSummary):
    walker_id: str
    name: str
    status: str | None = None
    photo: str | None = None
    flagged_reviews_count: int = 0


class AdminWalkerReputationListResponse(BaseModel):
    items: list[AdminWalkerReputationListItem]
    total: int
