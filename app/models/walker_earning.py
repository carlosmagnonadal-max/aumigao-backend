"""Ledger-fornecedor do passeador da REDE (Fase 2).

Uma entrada por passeio de REDE finalizado. O Aumigão DEVE este valor ao passeador
(que prestou serviço); liberação em cadência semanal via `payable_at` — desacoplada
do status do pagamento do tutor (dissolve o furo D+32). Passeio PRÓPRIO do tenant
NÃO entra aqui (é pago pelo pet shop).
"""
from sqlalchemy import Column, String, DateTime
from sqlalchemy.sql import func

from app.core.database import Base
from app.models.types import Money

WE_ACCRUED = "accrued"   # registrado; disponibilidade definida por payable_at
WE_VOID = "void"         # estornado (reembolso/disputa) — Fase futura


class WalkerEarning(Base):
    __tablename__ = "walker_earnings"

    id = Column(String, primary_key=True)
    walker_id = Column(String, nullable=False, index=True)
    tenant_id = Column(String, nullable=True, index=True)
    walk_id = Column(String, nullable=False, unique=True)  # idempotência: 1 por passeio

    gross = Column(Money, nullable=False)            # preço do passeio (medido)
    platform_amount = Column(Money, nullable=False)  # margem do Aumigão (rede)
    amount = Column(Money, nullable=False)           # fatia do passeador (o que devemos)

    status = Column(String, nullable=False, default=WE_ACCRUED, index=True)
    accrued_at = Column(DateTime(timezone=True), server_default=func.now())
    payable_at = Column(DateTime(timezone=True), nullable=False)  # quando vira "disponível"

    # Fase 3: estorno (void) — preenchidos quando status=WE_VOID.
    void_reason = Column(String, nullable=True)
    voided_at = Column(DateTime(timezone=True), nullable=True)
