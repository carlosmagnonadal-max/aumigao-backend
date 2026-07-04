"""Schemas para o CRUD self-service de unidades do tenant (admin)."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class TenantUnitAdminItem(BaseModel):
    id: str
    name: str
    slug: str
    enabled: bool
    created_at: datetime | None = None


class TenantUnitsAdminListResponse(BaseModel):
    units: list[TenantUnitAdminItem] = Field(default_factory=list)
    max_units: int | None  # None = ilimitado (Enterprise)
    used: int              # unidades ATIVAS


class TenantUnitCreatePayload(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)


class TenantUnitPatchPayload(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    enabled: bool | None = None
