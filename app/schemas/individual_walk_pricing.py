from pydantic import BaseModel, Field


# ----- Preço individual por tenant (admin) -----
class IndividualWalkPricingResponse(BaseModel):
    tenant_id: str
    price_30: float
    price_45: float
    price_60: float
    # Desconto flat (R$) quando o tutor leva o pet até o ponto de encontro.
    meeting_point_discount: float
    active: bool


class IndividualWalkPricingUpdate(BaseModel):
    price_30: float | None = Field(default=None, ge=0)
    price_45: float | None = Field(default=None, ge=0)
    price_60: float | None = Field(default=None, ge=0)
    meeting_point_discount: float | None = Field(default=None, ge=0)
    active: bool | None = None
