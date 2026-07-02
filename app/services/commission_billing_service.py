"""Acúmulo e faturamento da comissão medida do tenant (Fase 1).

Princípio: MEDIÇÃO ≠ CUSTÓDIA. O valor vem de Walk.price × taxa resolvida; o
Aumigão nunca toca no pagamento do tutor. Passeio de REDE não acumula aqui.
"""
import logging
import os
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy.orm import Session

from app.core.money import q2, to_float, to_money
from app.models.commission_entry import (
    CommissionEntry, COMM_ACCRUED, COMM_BILLED, COMM_PAID, COMM_VOID,
)

_logger = logging.getLogger("aumigao.commission_billing_service")


def _test_tenant_slugs() -> set[str]:
    """Slugs de tenants de TESTE que NÃO devem acumular/ser cobrados de comissão live.

    Lido em runtime de TEST_TENANT_SLUGS (CSV), default "pmg" (PetMagno = tenant de
    teste; ver memória tenants-reais-vs-teste). Conservador: só blinda o accrue,
    não inventa cobrança nova."""
    raw = os.getenv("TEST_TENANT_SLUGS", "pmg")
    return {s.strip().lower() for s in raw.split(",") if s.strip()}


def _is_test_tenant(db: Session, tenant_id: str | None) -> bool:
    if not tenant_id:
        return False
    try:
        from app.models.tenant import Tenant
        tenant = db.get(Tenant, tenant_id)
    except Exception:
        return False
    if tenant is None:
        return False
    # Flag explícita no model, se um dia existir (is_test/is_demo), tem prioridade.
    if getattr(tenant, "is_test", False) or getattr(tenant, "is_demo", False):
        return True
    return (getattr(tenant, "slug", "") or "").strip().lower() in _test_tenant_slugs()


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
    if getattr(walk, "is_referral_gift", False):
        return None  # brinde de indicação: plataforma não fatura comissão (decisão do fundador)
    if not getattr(walk, "tenant_id", None):
        return None
    # P2: tenant de teste (ex.: pmg/PetMagno) NÃO acumula comissão — blindagem
    # contra cobrança live de um tenant interno/de teste.
    if _is_test_tenant(db, walk.tenant_id):
        _logger.info(
            "accrue_commission_for_walk: pulado — tenant de teste tenant_id=%s walk_id=%s",
            walk.tenant_id, getattr(walk, "id", "?"),
        )
        return None
    price = float(getattr(walk, "price", 0) or 0)
    if price <= 0:
        return None
    existing = db.query(CommissionEntry).filter(CommissionEntry.walk_id == walk.id).first()
    if existing:
        return existing
    amount = to_float(q2(split.get("platform_amount", 0.0)))
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


def _next_period(period: str) -> str:
    """"YYYY-MM" → mês seguinte "YYYY-MM" (overflow de ano)."""
    try:
        year, month = (int(x) for x in period.split("-")[:2])
    except Exception:
        # fallback: mês atual (nunca deveria acontecer com period válido)
        now = datetime.now(timezone.utc)
        year, month = now.year, now.month
    month += 1
    if month > 12:
        month = 1
        year += 1
    return f"{year:04d}-{month:02d}"


def reverse_commission_for_walk(db: Session, walk_id: str, *, reason: str) -> "CommissionEntry | None":
    """Reverte (idempotente) a comissão do tenant de um passeio estornado (COMM_VOID).

    Chamado no refund/chargeback (webhook, quando há walk_id) e no void manual do admin.
    Dois casos:
      - entrada ainda COMM_ACCRUED (mês não faturado): vira COMM_VOID → não entra no
        faturamento. Sem cobrança do tenant por passeio estornado.
      - entrada já COMM_BILLED/COMM_PAID (mês já cobrado): NÃO apaga. Gera um AJUSTE
        negativo (entrada COMM_ACCRUED com amount<0) no PERÍODO SEGUINTE, que o
        faturamento mensal soma → o tenant é creditado no próximo mês.

    Idempotência: o ajuste usa walk_id sintético único (comm-void-adj-<walk_id>);
    reverter 2x o mesmo passeio não gera crédito dobrado. Não faz commit.
    Retorna a entrada afetada (a void ou o ajuste), ou None se nada a fazer.
    """
    entry = db.query(CommissionEntry).filter(CommissionEntry.walk_id == walk_id).first()
    if entry is None:
        return None
    if entry.status == COMM_VOID:
        return None  # já revertida

    if entry.status == COMM_ACCRUED:
        entry.status = COMM_VOID
        _logger.info(
            "reverse_commission_for_walk: entrada accrued -> void walk_id=%s tenant_id=%s reason=%s",
            walk_id, entry.tenant_id, reason,
        )
        return entry

    # Já faturada/paga: cria ajuste (crédito) no período seguinte, idempotente.
    adj_walk_id = f"comm-void-adj-{walk_id}"
    existing_adj = db.query(CommissionEntry).filter(CommissionEntry.walk_id == adj_walk_id).first()
    if existing_adj is not None:
        return existing_adj  # já ajustado — não duplica crédito

    adjustment = CommissionEntry(
        id=str(uuid4()),
        tenant_id=entry.tenant_id,
        walk_id=adj_walk_id,
        period=_next_period(entry.period),
        walk_price=0.0,
        commission_percent=0.0,
        amount=to_float(q2(-to_money(entry.amount or 0))),  # crédito (negativo)
        is_network=False,
        status=COMM_ACCRUED,
    )
    db.add(adjustment)
    _logger.warning(
        "reverse_commission_for_walk: entrada %s já faturada — ajuste de crédito %.2f no período %s "
        "walk_id=%s tenant_id=%s reason=%s",
        entry.status, adjustment.amount, adjustment.period, walk_id, entry.tenant_id, reason,
    )
    return adjustment


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

    # Risco residual: se o commit falhar APÓS o charge no Asaas, as entradas seguem
    # accrued e podem ser recobradas no próximo run. Mitigação completa
    # (idempotency key/intent record) fica pra Fase 2.
    """
    from app.models.tenant import Tenant

    # Pré-check anti-cobrança-dupla: se já existe ao menos uma entrada COMM_BILLED
    # com asaas_payment_id para este tenant+period, retorna o id existente sem cobrar
    # de novo. Torna re-execuções após falha parcial multi-tenant seguras.
    already = db.query(CommissionEntry).filter(
        CommissionEntry.tenant_id == tenant_id,
        CommissionEntry.period == period,
        CommissionEntry.status == COMM_BILLED,
        CommissionEntry.asaas_payment_id.isnot(None),
    ).first()
    if already:
        return already.asaas_payment_id

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
    total_d = q2(sum((to_money(r.amount) for r in rows), to_money(0)))
    total = to_float(total_d)
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
    """Fatura todos os tenants com comissão `accrued` no período. Retorna ids das cobranças.

    Cada tenant é processado de forma isolada: após faturamento bem-sucedido,
    faz db.commit() imediatamente para persistir o `billed` daquele tenant antes
    de prosseguir. Se charge_fn levantar exceção para um tenant, faz db.rollback()
    desse tenant, loga o erro e continua para os próximos — evitando cobrança dupla
    no próximo run (tenants já persistidos não são reprocessados).
    """
    tenant_ids = [
        row[0]
        for row in db.query(CommissionEntry.tenant_id)
        .filter(CommissionEntry.period == period, CommissionEntry.status == COMM_ACCRUED)
        .group_by(CommissionEntry.tenant_id)
        .all()
    ]
    out: list[str] = []
    for tid in tenant_ids:
        try:
            cid = bill_tenant_commission(db, tid, period, charge_fn=charge_fn)
            if cid:
                db.commit()
                out.append(cid)
        except Exception as exc:
            db.rollback()
            _logger.error(
                "commission_billing: falha ao faturar tenant=%s period=%s erro=%s",
                tid, period, exc,
            )
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


# ---------------------------------------------------------------------------
# Task 6: adaptador de cobrança avulsa Asaas (produção)
# ---------------------------------------------------------------------------

def make_asaas_charge_fn():
    """Retorna um charge_fn que cria uma cobrança avulsa PIX no Asaas.

    Reusa ensure_tenant_asaas_customer (Projeto B) para obter/criar o customer_id
    e a configuração de gateway (_get_asaas_config) já usada em toda a rota de
    pagamentos. A cobrança usa externalReference='tenant_comm:<tenant_id>:<period>'
    para que o webhook a reconheça e roteie corretamente.

    Contrato de charge_fn:
        charge_fn(db, tenant, total, period, description) -> asaas_payment_id (str)

    ensure_tenant_asaas_customer é async — chamado via asyncio.run() pois o
    endpoint interno (/internal/commission-billing/run) é síncrono, igual ao
    sweep do Projeto B (saas_billing_sweep).
    """
    import asyncio
    import logging
    import httpx
    from datetime import date, timedelta
    from fastapi import HTTPException

    _logger = logging.getLogger("aumigao.commission_billing_service.asaas_adapter")

    def charge_fn(db, tenant, total: float, period: str, description: str) -> str:
        """Cria cobrança PIX avulsa no Asaas para a comissão medida do tenant."""
        # 1. Obtém/cria customer Asaas do tenant (idempotente, commita o customer_id)
        from app.services.tenant_saas_billing_service import ensure_tenant_asaas_customer
        try:
            customer_id = asyncio.run(ensure_tenant_asaas_customer(db, tenant))
        except RuntimeError:
            # Python ≥3.12: get_event_loop() levanta RuntimeError quando não há loop;
            # cria um loop novo explicitamente para garantir compatibilidade.
            import asyncio as _asyncio
            _loop = _asyncio.new_event_loop()
            try:
                customer_id = _loop.run_until_complete(ensure_tenant_asaas_customer(db, tenant))
            finally:
                _loop.close()

        # 2. Obtém configuração do gateway (reutiliza _get_asaas_config de payments.py)
        from app.routes.payments import _get_asaas_config
        cfg = _get_asaas_config()
        base_url: str = cfg["base_url"]
        api_key: str = cfg["api_key"]
        is_live: bool = cfg["is_live"]
        mode_label = "live" if is_live else "sandbox"

        external_reference = f"tenant_comm:{tenant.id}:{period}"
        due_date = str(date.today() + timedelta(days=1))

        payment_payload = {
            "customer": customer_id,
            "billingType": "PIX",
            "value": total,
            "dueDate": due_date,
            "description": description,
            "externalReference": external_reference,
        }

        _logger.info(
            "commission_charge: criando cobrança avulsa tenant=%s period=%s total=%s mode=%s",
            tenant.id, period, total, mode_label,
        )

        try:
            import asyncio as _asyncio

            async def _post():
                async with httpx.AsyncClient(
                    base_url=base_url,
                    headers={
                        "access_token": api_key,
                        "Content-Type": "application/json",
                        "User-Agent": f"Aumigao Commission {mode_label.capitalize()}",
                    },
                    timeout=20,
                ) as client:
                    response = await client.post("/payments", json=payment_payload)
                    if response.status_code >= 400:
                        try:
                            err = response.json()
                        except Exception:
                            err = {"raw": response.text}
                        msg = (
                            (err.get("errors") or [{}])[0].get("description")
                            or err.get("description")
                            or "Erro desconhecido"
                        )
                        _logger.error(
                            "commission_charge: Asaas error tenant=%s status=%s body=%s",
                            tenant.id, response.status_code, err,
                        )
                        raise HTTPException(
                            status_code=502,
                            detail=f"Falha ao criar cobrança de comissão no gateway: {msg}",
                        )
                    data = response.json()
                    payment_id = data["id"]
                    _logger.info(
                        "commission_charge: cobrança criada payment_id=%s tenant=%s period=%s",
                        payment_id, tenant.id, period,
                    )
                    return payment_id

            try:
                return _asyncio.run(_post())
            except RuntimeError:
                # Python ≥3.12: get_event_loop() levanta RuntimeError quando não há loop;
                # cria um loop novo explicitamente para garantir compatibilidade.
                _loop = _asyncio.new_event_loop()
                try:
                    return _loop.run_until_complete(_post())
                finally:
                    _loop.close()

        except HTTPException:
            raise
        except Exception as exc:
            _logger.exception(
                "commission_charge: erro de rede tenant=%s: %s", tenant.id, exc
            )
            raise HTTPException(
                status_code=502,
                detail="Gateway de pagamento indisponível ao cobrar comissão. Tente novamente.",
            )

    return charge_fn
