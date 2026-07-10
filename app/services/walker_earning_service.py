"""Serviço do ledger-fornecedor do passeador da rede (Fase 2)."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy.orm import Session

from app.core.money import q2, to_float, to_money
from app.models.walker_earning import WalkerEarning, WE_ACCRUED, WE_VOID


def compute_payable_at(completion_dt: datetime) -> datetime:
    """Cadência SEMANAL: ganhos de passeios concluídos numa semana (seg–dom)
    ficam disponíveis na QUARTA-FEIRA da semana SEGUINTE.

    Determinístico (não usa 'now'): depende só da data de conclusão.
    Retorna datetime tz-aware (UTC) à meia-noite da quarta-feira alvo.
    """
    d = completion_dt.date()
    monday_this_week = d - timedelta(days=d.weekday())  # weekday(): seg=0
    wednesday_next_week = monday_this_week + timedelta(days=7 + 2)
    return datetime(
        wednesday_next_week.year, wednesday_next_week.month, wednesday_next_week.day,
        tzinfo=timezone.utc,
    )


def _completion_dt_from_walk(walk) -> datetime:
    """Deriva a data de conclusão do passeio (scheduled_date 'YYYY-MM-DD[THH:MM]' ou created_at, fallback now)."""
    sd = getattr(walk, "scheduled_date", None)
    if sd and isinstance(sd, str) and len(sd) >= 10:
        try:
            return datetime.fromisoformat(sd[:16]) if "T" in sd else datetime.fromisoformat(sd[:10])
        except ValueError:
            pass
    created = getattr(walk, "created_at", None)
    if isinstance(created, datetime):
        return created
    return datetime.now(timezone.utc)


def accrue_walker_earning(db: Session, walk, split: dict) -> "WalkerEarning | None":
    """Cria (idempotente) a entrada de ganho do passeador da REDE.

    amount = fatia do passeador (split['walker_amount']); platform_amount = margem.
    payable_at = cadência semanal. Não faz commit (caller comita).
    Só deve ser chamado para passeio de REDE (o caller decide via is_network_walk).
    """
    price = float(getattr(walk, "price", 0) or 0)
    if price <= 0:
        return None
    existing = db.query(WalkerEarning).filter(WalkerEarning.walk_id == walk.id).first()
    if existing:
        return existing
    completion = _completion_dt_from_walk(walk)
    comp = completion if completion.tzinfo else completion.replace(tzinfo=timezone.utc)
    earning = WalkerEarning(
        id=str(uuid4()),
        walker_id=walk.walker_id or getattr(walk, "assigned_walker_id", None),
        tenant_id=walk.tenant_id,
        walk_id=walk.id,
        gross=price,
        platform_amount=to_float(q2(split.get("platform_amount", 0.0))),
        amount=to_float(q2(split.get("walker_amount", 0.0))),
        status=WE_ACCRUED,
        payable_at=compute_payable_at(comp),
    )
    db.add(earning)
    return earning


def accrue_cancellation_compensation(db: Session, walk, walker_id: str, amount: float) -> "WalkerEarning | None":
    """Cria (idempotente) o ganho PENDENTE do walker por compensação de cancelamento
    tardio (mig 0107 — cancel_walk_service).

    Diferente de accrue_walker_earning: `amount` é a compensação (taxa retida ×
    walker_share%), NÃO o preço do passeio — o walk foi CANCELADO, não concluído,
    então gross == amount e platform_amount == 0 (a plataforma não fica com
    margem aqui; o que sobrou da taxa retida fica com o tenant/plataforma via a
    própria retenção do estorno parcial, contabilizada no Payment). SEM
    commission_entry: comissão mede serviço prestado — aqui não houve.

    Idempotente pelo walk_id UNIQUE de WalkerEarning: um walk cancelado nunca é
    concluído depois, então nunca colide com um accrue_walker_earning legítimo.
    payable_at usa a cadência semanal normal a partir de AGORA (não da
    scheduled_date — o "serviço" que gera a compensação é o próprio cancelamento).
    Não faz commit (caller comita).
    """
    if amount <= 0:
        return None
    existing = db.query(WalkerEarning).filter(WalkerEarning.walk_id == walk.id).first()
    if existing:
        return existing
    now = datetime.now(timezone.utc)
    earning = WalkerEarning(
        id=str(uuid4()),
        walker_id=walker_id,
        tenant_id=walk.tenant_id,
        walk_id=walk.id,
        gross=amount,
        platform_amount=0.0,
        amount=amount,
        status=WE_ACCRUED,
        payable_at=compute_payable_at(now),
    )
    db.add(earning)
    return earning


def network_earnings_by_tenant(db: Session, walker_id: str, now: datetime | None = None) -> dict:
    """Agrega o ledger do passeador por tenant_id.

    Retorna { tenant_id: {"available": x, "areceber": y} }.
    available = earnings com payable_at <= now; areceber = payable_at > now.
    Exclui status void.
    Cuida de tz: se payable_at vier naive, trata como UTC.
    """
    now = now or datetime.now(timezone.utc)
    rows = (
        db.query(WalkerEarning)
        .filter(WalkerEarning.walker_id == walker_id, WalkerEarning.status != WE_VOID)
        .all()
    )
    out: dict = {}
    for r in rows:
        b = out.setdefault(r.tenant_id, {"available": to_money(0), "areceber": to_money(0)})
        pa = r.payable_at
        if pa is not None and pa.tzinfo is None:
            pa = pa.replace(tzinfo=timezone.utc)
        if pa is not None and pa <= now:
            b["available"] += to_money(r.amount or 0)
        else:
            b["areceber"] += to_money(r.amount or 0)
    # Borda: soma em Decimal, entrega float (contrato dos consumidores/endpoints).
    return {
        tid: {"available": to_float(q2(v["available"])), "areceber": to_float(q2(v["areceber"]))}
        for tid, v in out.items()
    }
