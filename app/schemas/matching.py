from datetime import datetime

from pydantic import BaseModel, Field


class MatchingWalkerRequest(BaseModel):
    pet_id: str | None = None
    scheduled_at: str | None = None
    duration_minutes: int = 45
    pickup_method: str = "home_pickup"
    modality: str = "standard"
    address_id: str | None = None
    neighborhood: str | None = None
    city: str | None = None
    latitude: float | None = None
    longitude: float | None = None


class MatchingContext(BaseModel):
    city: str | None = None
    neighborhood: str | None = None
    scheduled_at: str | None = None
    duration_minutes: int


class PublicMatchedWalker(BaseModel):
    walker_id: str
    name: str
    photo_url: str | None = None
    rating_average: float
    reviews_count: int
    total_walks: int
    level: str
    distance_km: float | None = None
    estimated_arrival_minutes: int | None = None
    badges: list[str]
    display_reason: str
    can_select: bool = True


class MatchingResponse(BaseModel):
    top_recommended: list[PublicMatchedWalker]
    other_options: list[PublicMatchedWalker]
    total_found: int
    matching_context: MatchingContext


class MatchingDebugItem(PublicMatchedWalker):
    proximity_score: float
    rating_score: float
    experience_score: float
    availability_score: float
    matching_score_base: float
    behavior_score: float
    boost_score: float
    final_matching_score: float
    risk_level: str | None = None
    eligibility_notes: list[str] = Field(default_factory=list)


class MatchingDebugResponse(BaseModel):
    items: list[MatchingDebugItem]
    total_found: int
    matching_context: MatchingContext


class WalkerBoostUpdate(BaseModel):
    boost_enabled: bool = False
    boost_type: str | None = "admin_boost"
    boost_score: int = Field(default=0, ge=0, le=5)
    boost_start_at: datetime | None = None
    boost_end_at: datetime | None = None
    boost_reason: str | None = None
    boost_status: str | None = "inactive"


class WalkerBoostResponse(BaseModel):
    walker_id: str
    walker_name: str
    status: str | None = None
    rating_average: float
    reviews_count: int
    total_walks: int
    boost_enabled: bool
    boost_type: str | None = None
    boost_score: int
    boost_start_at: datetime | None = None
    boost_end_at: datetime | None = None
    boost_reason: str | None = None
    boost_status: str
    can_apply_boost: bool
    eligibility_reason: str


class WalkerBoostListResponse(BaseModel):
    items: list[WalkerBoostResponse]
    total: int
