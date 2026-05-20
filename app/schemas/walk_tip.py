from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel


class WalkTipCheckoutCreate(BaseModel):
    amount: float = Field(gt=0, le=500)


class WalkTipCheckoutResponse(ORMModel):
    tip_id: str
    checkout_url: str | None = None
    status: str
    provider: str
    amount: float
    created_at: datetime
    paid_at: datetime | None = None
