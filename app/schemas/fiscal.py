from pydantic import BaseModel, Field, field_validator
from app.schemas.common import ORMModel
from app.models.fiscal import ALLOWED_TAX_REGIMES

class FiscalConfigResponse(ORMModel):
    tenant_id: str
    tax_regime: str | None = None
    commission_tax_percent: float
    subscription_tax_percent: float
    walker_tax_percent: float
    iss_percent: float | None = None
    municipal_service_code: str | None = None
    cnae: str | None = None
    service_description: str | None = None
    active: bool

class FiscalConfigUpdate(BaseModel):
    tax_regime: str | None = None
    commission_tax_percent: float | None = Field(default=None, ge=0, le=100)
    subscription_tax_percent: float | None = Field(default=None, ge=0, le=100)
    walker_tax_percent: float | None = Field(default=None, ge=0, le=100)
    iss_percent: float | None = Field(default=None, ge=0, le=100)
    municipal_service_code: str | None = None
    cnae: str | None = None
    service_description: str | None = None
    active: bool | None = None

    @field_validator("tax_regime")
    @classmethod
    def validate_tax_regime(cls, v: str | None) -> str | None:
        if v is not None and v not in ALLOWED_TAX_REGIMES:
            raise ValueError(
                f"tax_regime inválido: {v!r}. Valores permitidos: {ALLOWED_TAX_REGIMES}"
            )
        return v
