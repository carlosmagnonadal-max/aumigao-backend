from pydantic import BaseModel, Field


# ----- Preço individual por tenant (admin) -----
class IndividualWalkPricingResponse(BaseModel):
    tenant_id: str
    price_30: float
    price_45: float
    price_60: float
    active: bool


class IndividualWalkPricingUpdate(BaseModel):
    price_30: float | None = Field(default=None, ge=0)
    price_45: float | None = Field(default=None, ge=0)
    price_60: float | None = Field(default=None, ge=0)
    active: bool | None = None
