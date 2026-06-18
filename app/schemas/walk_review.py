from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel


ALLOWED_WALK_REVIEW_TAGS = {
    "punctual",
    "caring",
    "communication",
    "pet_comfort",
    "excellent_walk",
}


class WalkReviewCreate(BaseModel):
    # Sec-P3: max_length defensivo no campo de texto livre.
    rating: int = Field(ge=1, le=5)
    comment: str | None = Field(None, max_length=2000)
    tags: list[str] = Field(default_factory=list)


class WalkReviewResponse(ORMModel):
    id: str
    walk_id: str
    tutor_id: str
    walker_id: str
    rating: int
    comment: str | None = None
    tags: list[str] = Field(default_factory=list)
    created_at: datetime
