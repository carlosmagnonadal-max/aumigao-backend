from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel


class CouponResponse(ORMModel):
    id: str
    tenant_id: str
    code: str
    discount_type: str
    discount_value: float
    min_amount: float
    max_uses: int | None = None
    max_uses_per_user: int | None = None
    uses_count: int
    active: bool
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    created_at: datetime


class CouponCreate(BaseModel):
    code: str = Field(min_length=1)
    discount_type: str = "percent"  # percent | fixed
    discount_value: float = Field(ge=0)
    min_amount: float = Field(default=0, ge=0)
    max_uses: int | None = Field(default=None, ge=1)
    max_uses_per_user: int | None = Field(default=1, ge=1)
    active: bool = True
    valid_from: datetime | None = None
    valid_until: datetime | None = None


class CouponUpdate(BaseModel):
    code: str | None = Field(default=None, min_length=1)
    discount_type: str | None = None
    discount_value: float | None = Field(default=None, ge=0)
    min_amount: float | None = Field(default=None, ge=0)
    max_uses: int | None = Field(default=None, ge=1)
    max_uses_per_user: int | None = Field(default=None, ge=1)
    active: bool | None = None
    valid_from: datetime | None = None
    valid_until: datetime | None = None


class CouponValidateRequest(BaseModel):
    code: str
    amount: float = Field(ge=0)


class CouponValidateResult(BaseModel):
    valid: bool
    code: str
    discount_amount: float = 0.0
    final_amount: float = 0.0
    message: str | None = None
