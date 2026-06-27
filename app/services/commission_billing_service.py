"""Acúmulo e faturamento da comissão medida do tenant (Fase 1).

Princípio: MEDIÇÃO ≠ CUSTÓDIA. O valor vem de Walk.price × taxa resolvida; o
Aumigão nunca toca no pagamento do tutor. Passeio de REDE não acumula aqui.
"""
from datetime import datetime, timezone
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


# ---------------------------------------------------------------------------
# Task 5: faturamento mensal
# ---------------------------------------------------------------------------

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def bill_tenant_commission(
    db: Session, tenant_id: str, period: str, *, charge_fn
) -> "str | None":
    """Soma as entradas `accrued` do tenant no período, emite UMA cobrança via
    `charge_fn` e marca as entradas como `billed`. Retorna o id da cobrança ou None.

    `charge_fn(db, tenant, total, period, description) -> asaas_payment_id` é injetável
    (testes passam fake; produção passa o adaptador Asaas — ver Task 6).
    Não faz commit.
    """
    from app.models.tenant import Tenant

    rows = (
        db.query(CommissionEntry)
        .filter(
            CommissionEntry.tenant_id == tenant_id,
            CommissionEntry.period == period,
            CommissionEntry.status == COMM_ACCRUED,
        )
        .all()
    )
    if not rows:
        return None
    total = round(sum(float(r.amount) for r in rows), 2)
    if total <= 0:
        return None
    tenant = db.get(Tenant, tenant_id)
    description = f"Comissão de uso Aumigão — {period} ({len(rows)} passeios)"
    asaas_payment_id = charge_fn(db, tenant, total, period, description)
    now = _now_utc()
    for r in rows:
        r.status = COMM_BILLED
        r.asaas_payment_id = asaas_payment_id
        r.billed_at = now
    return asaas_payment_id


def run_monthly_commission_billing(
    db: Session, period: str, *, charge_fn
) -> "list[str]":
    """Fatura todos os tenants com comissão `accrued` no período. Retorna ids das cobranças."""
    tenant_ids = [
        row[0]
        for row in db.query(CommissionEntry.tenant_id)
        .filter(CommissionEntry.period == period, CommissionEntry.status == COMM_ACCRUED)
        .group_by(CommissionEntry.tenant_id)
        .all()
    ]
    out: list[str] = []
    for tid in tenant_ids:
        cid = bill_tenant_commission(db, tid, period, charge_fn=charge_fn)
        if cid:
            out.append(cid)
    return out


def mark_commission_paid(db: Session, asaas_payment_id: str) -> int:
    """Webhook: marca como `paid` todas as entradas faturadas por esta cobrança.
    Retorna quantas linhas mudaram. Idempotente. Não faz commit."""
    rows = (
        db.query(CommissionEntry)
        .filter(
            CommissionEntry.asaas_payment_id == asaas_payment_id,
            CommissionEntry.status == COMM_BILLED,
        )
        .all()
    )
    now = _now_utc()
    for r in rows:
        r.status = COMM_PAID
        r.paid_at = now
    return len(rows)
