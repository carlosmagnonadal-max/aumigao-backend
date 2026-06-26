from pydantic import BaseModel, Field
from app.schemas.common import ORMModel

class FiscalConfigResponse(ORMModel):
    tenant_id: str
    commission_tax_percent: float
    subscription_tax_percent: float
    walker_tax_percent: float
    iss_percent: float | None = None
    municipal_service_code: str | None = None
    simples_nacional: bool | None = None
    cnae: str | None = None
    service_description: str | None = None
    active: bool

class FiscalConfigUpdate(BaseModel):
    commission_tax_percent: float | None = Field(default=None, ge=0, le=100)
    subscription_tax_percent: float | None = Field(default=None, ge=0, le=100)
    walker_tax_percent: float | None = Field(default=None, ge=0, le=100)
    iss_percent: float | None = Field(default=None, ge=0, le=100)
    municipal_service_code: str | None = None
    simples_nacional: bool | None = None
    cnae: str | None = None
    service_description: str | None = None
    active: bool | None = None
