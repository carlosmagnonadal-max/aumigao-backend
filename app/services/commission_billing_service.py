"""Acúmulo e faturamento da comissão medida do tenant (Fase 1).

Princípio: MEDIÇÃO ≠ CUSTÓDIA. O valor vem de Walk.price × taxa resolvida; o
Aumigão nunca toca no pagamento do tutor. Passeio de REDE não acumula aqui.
"""
from uuid import uuid4

from sqlalchemy.orm import Session

from app.models.commission_entry import (
    CommissionEntry, COMM_ACCRUED, COMM_BILLED, COMM_PAID,
)


def accrue_commission_for_walk(
    db: Session, walk, split: dict, *, is_network: bool, period: str
) -> "CommissionEntry | None":
    """Cria (idempotente) a entrada de comissão para um passeio finalizado.

    - Só acumula passeio de passeador PRÓPRIO (is_network=False).
    - Não acumula preço zero.
    - Idempotente por walk_id (uq constraint + checagem prévia).
    Não faz commit — o caller comita junto da finalização.
    """
    if is_network:
        return None
    price = float(getattr(walk, "price", 0) or 0)
    if price <= 0:
        return None
    existing = db.query(CommissionEntry).filter(CommissionEntry.walk_id == walk.id).first()
    if existing:
        return existing
    amount = round(float(split.get("platform_amount", 0.0)), 2)
    entry = CommissionEntry(
        id=str(uuid4()),
        tenant_id=walk.tenant_id,
        walk_id=walk.id,
        period=period,
        walk_price=price,
        commission_percent=float(split.get("commission_percent", 0.0)),
        amount=amount,
        is_network=False,
        status=COMM_ACCRUED,
    )
    db.add(entry)
    return entry
