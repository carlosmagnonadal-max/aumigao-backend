"""Schema da disponibilidade semanal do passeador (WK-01).

Substitui o `payload: dict` solto do PUT /walker/availability por um contrato
tipado: schedule = { dia: { enabled, slots[] } } (espelha o shape da tela).
"""
from pydantic import BaseModel, Field


class WalkerDaySchedule(BaseModel):
    enabled: bool = False
    slots: list[str] = Field(default_factory=list)


class WalkerAvailabilityUpdate(BaseModel):
    schedule: dict[str, WalkerDaySchedule]
