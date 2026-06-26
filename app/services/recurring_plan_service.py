"""Regras de negócio dos planos recorrentes (Onda 1).

Catálogo por tenant + ciclo de vida da assinatura do tutor + concessão de
créditos por ciclo. A cobrança recorrente real via API nativa do Asaas é
gerenciada por asaas_subscription_service (Fase 7 $-2).
"""
import logging
from datetime import datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import update as sa_update
from sqlalchemy.orm import Session

from app.models.recurring_plan import (
    RECURRING_PLANS_FEATURE_KEY,
    SUBSCRIPTION_ACTIVE,
    SUBSCRIPTION_CANCELLED,
    RecurringPlan,
    TutorSubscription,
)
from app.models.tenant import Tenant
from app.services.tenant_plan_service import enforce_tenant_product_feature, tenant_has_feature

FEATURE_LABEL = "Planos recorrentes"
logger = logging.getLogger("aumigao.recurring_plan_service")


def _period_end(now: datetime, interval: str) -> datetime:
    """Retorna a data de fim do período baseada no interval do plano.

    Suporta: monthly (+1 mês), semiannual (+6 meses), yearly (+12 meses),
    quarterly (+3 meses), weekly (+7 dias), biweekly (+14 dias).
    Default: +1 mês (mesmo que monthly).

    Não usa python-dateutil; soma meses manualmente com tratamento correto de
    overflow (ex.: 31 de janeiro + 1 mês → 28/29 de fevereiro).
    """
    if interval in ("weekly",):
        return now + timedelta(days=7)
    if interval in ("biweekly",):
        return now + timedelta(days=14)

    # Intervalos baseados em meses
    month_offsets = {
        "monthly": 1,
        "quarterly": 3,
        "semiannual": 6,
        "yearly": 12,
    }
    months = month_offsets.get(interval, 1)  # default: mensal

    target_month = now.month + months
    target_year = now.year + (target_month - 1) // 12
    target_month = (target_month - 1) % 12 + 1

    # Overflow de dia: ex. 31/jan + 1m → clamp para último dia do mês alvo
    import calendar
    max_day = calendar.monthrange(target_year, target_month)[1]
    target_day = min(now.day, max_day)

    return now.replace(year=target_year, month=target_month, day=target_day)

# Importações lazy-safe: podem ser sobrescritas em testes via patch no namespace deste módulo.
try:
    import httpx  # noqa: F401
    from app.services.asaas_subscription_service import (
        cancel_asaas_subscription,
        create_asaas_subscription,
    )
    from app.routes.payments import (
        _get_asaas_config,
        asaas_headers,
        create_asaas_customer,
    )
except Exception:  # pragma: no cover — falha em init (circular ou ausente)
    httpx = None  # type: ignore[assignment]
    cancel_asaas_subscription = None  # type: ignore[assignment]
    create_asaas_subscription = None  # type: ignore[assignment]
    _get_asaas_config = None  # type: ignore[assignment]
    asaas_headers = None  # type: ignore[assignment]
    create_asaas_customer = None  # type: ignore[assignment]


def recurring_plans_enabled(tenant: Tenant, db: Session) -> bool:
    return tenant_has_feature(tenant, db, RECURRING_PLANS_FEATURE_KEY)


def enforce_enabled(tenant: Tenant, db: Session) -> None:
    enforce_tenant_product_feature(tenant, db, RECURRING_PLANS_FEATURE_KEY, FEATURE_LABEL)


def list_plans(db: Session, tenant_id: str, *, only_active: bool) -> list[RecurringPlan]:
    query = db.query(RecurringPlan).filter(RecurringPlan.tenant_id == tenant_id)
    if only_active:
        query = query.filter(RecurringPlan.active.is_(True))
    return query.order_by(RecurringPlan.price.asc()).all()


def get_plan_or_404(db: Session, tenant_id: str, plan_id: str) -> RecurringPlan:
    plan = (
        db.query(RecurringPlan)
        .filter(RecurringPlan.tenant_id == tenant_id, RecurringPlan.id == plan_id)
        .first()
    )
    if not plan:
        raise HTTPException(status_code=404, detail="Plano recorrente não encontrado.")
    return plan


def get_active_subscription(db: Session, tenant_id: str, tutor_id: str) -> TutorSubscription | None:
    return (
        db.query(TutorSubscription)
        .filter(
            TutorSubscription.tenant_id == tenant_id,
            TutorSubscription.tutor_id == tutor_id,
            TutorSubscription.status == SUBSCRIPTION_ACTIVE,
        )
        .order_by(TutorSubscription.created_at.desc())
        .first()
    )


def consume_credit_if_available(db: Session, tenant: Tenant, tutor_id: str) -> TutorSubscription | None:
    """Consome 1 crédito da assinatura ativa do tutor, de forma ATÔMICA.

    Usa UPDATE ... WHERE credits_remaining > 0 (condicional no banco) para evitar
    double-spend em requisições concorrentes — dois POST /walks simultâneos do mesmo
    tutor não conseguem consumir o mesmo último crédito.

    Retorna a TutorSubscription (recarregada, com credits_remaining já decrementado;
    sem commit — o caller commita) ou None quando não há assinatura ativa com crédito.
    """
    result = db.execute(
        sa_update(TutorSubscription)
        .where(
            TutorSubscription.tenant_id == tenant.id,
            TutorSubscription.tutor_id == tutor_id,
            TutorSubscription.status == SUBSCRIPTION_ACTIVE,
            TutorSubscription.credits_remaining > 0,
        )
        .values(
            credits_remaining=TutorSubscription.credits_remaining - 1,
            updated_at=datetime.utcnow(),
        )
        .returning(TutorSubscription.id)
    )
    row = result.first()
    if row is None:
        return None
    db.expire_all()
    return db.get(TutorSubscription, row[0])


def subscribe(db: Session, tenant: Tenant, tutor_id: str, plan_id: str) -> TutorSubscription:
    """Versão síncrona (legada) — usada pelos testes existentes.

    Não chama o Asaas. Para integração Asaas use subscribe_async.
    """
    enforce_enabled(tenant, db)
    plan = get_plan_or_404(db, tenant.id, plan_id)
    if not plan.active:
        raise HTTPException(status_code=409, detail="Este plano não está disponível para assinatura.")

    now = datetime.utcnow()
    # Mantém uma assinatura ativa por tutor: cancela a anterior (troca de plano).
    existing = get_active_subscription(db, tenant.id, tutor_id)
    if existing:
        existing.status = SUBSCRIPTION_CANCELLED
        existing.cancelled_at = now
        existing.updated_at = now
        db.add(existing)

    subscription = TutorSubscription(
        tenant_id=tenant.id,
        plan_id=plan.id,
        tutor_id=tutor_id,
        status=SUBSCRIPTION_ACTIVE,
        price=plan.price,
        walks_per_cycle=plan.walks_per_cycle,
        credits_remaining=plan.walks_per_cycle,
        current_period_start=now,
        current_period_end=_period_end(now, plan.interval),
    )
    db.add(subscription)
    db.commit()
    db.refresh(subscription)
    return subscription


async def subscribe_async(
    db: Session,
    tenant: Tenant,
    tutor_id: str,
    plan_id: str,
    *,
    tutor_user=None,
) -> TutorSubscription:
    """Versão assíncrona: cria assinatura local + subscription nativa no Asaas.

    Regra sem zumbi:
    1. Prepara objeto local (sem commit).
    2. Tenta criar no Asaas — se falhar, levanta 502 sem salvar nada.
    3. Salva local com asaas_subscription_id preenchido.

    tutor_user: instância de User do tutor (necessária para criar customer no Asaas).
    Se não fornecido, cria sem customer Asaas (fallback gracioso).
    """
    # Usa importações no namespace do módulo (patcháveis em testes via patch em
    # app.services.recurring_plan_service.create_asaas_subscription etc.).
    # Fallback para None quando módulo não carregou (testes mínimos sem payments).
    _create_sub = create_asaas_subscription
    _create_cust = create_asaas_customer
    _get_cfg = _get_asaas_config
    _asaas_hdrs = asaas_headers
    _httpx = httpx

    enforce_enabled(tenant, db)
    plan = get_plan_or_404(db, tenant.id, plan_id)
    if not plan.active:
        raise HTTPException(status_code=409, detail="Este plano não está disponível para assinatura.")

    now = datetime.utcnow()
    # Cancela assinatura anterior (troca de plano)
    existing = get_active_subscription(db, tenant.id, tutor_id)
    if existing:
        # Cancela no Asaas antes de cancelar localmente
        if existing.asaas_subscription_id and cancel_asaas_subscription is not None:
            try:
                await cancel_asaas_subscription(existing.asaas_subscription_id)
            except Exception:
                logger.warning(
                    "falha ao cancelar assinatura anterior no Asaas id=%s — continuando troca de plano",
                    existing.asaas_subscription_id,
                )
        existing.status = SUBSCRIPTION_CANCELLED
        existing.cancelled_at = now
        existing.updated_at = now
        db.add(existing)

    subscription = TutorSubscription(
        tenant_id=tenant.id,
        plan_id=plan.id,
        tutor_id=tutor_id,
        status=SUBSCRIPTION_ACTIVE,
        price=plan.price,
        walks_per_cycle=plan.walks_per_cycle,
        credits_remaining=plan.walks_per_cycle,
        current_period_start=now,
        current_period_end=_period_end(now, plan.interval),
    )
    db.add(subscription)
    db.flush()  # Gera ID sem commit

    # Cria subscription no Asaas (pode levantar 502)
    asaas_sub_id: str | None = None
    if tutor_user is not None and _get_cfg is not None and _httpx is not None:
        try:
            cfg = _get_cfg()
            from app.models.tutor_profile import TutorProfile
            _tp = db.query(TutorProfile).filter(TutorProfile.user_id == tutor_id).first()
            tutor_cpf_raw = (_tp.cpf or "").strip() if _tp else ""
            tutor_cpf = tutor_cpf_raw if len(tutor_cpf_raw) == 11 else None

            async with _httpx.AsyncClient(
                base_url=cfg["base_url"],
                headers=_asaas_hdrs(cfg["api_key"], mode="live" if cfg["is_live"] else "sandbox"),
                timeout=20,
            ) as client:
                customer_id = await _create_cust(
                    client, tutor_user, is_live=cfg["is_live"], tutor_cpf=tutor_cpf
                )

            asaas_sub_id = await _create_sub(
                customer_id=customer_id,
                value=plan.price,
                interval=plan.interval,
                tutor_subscription_id=subscription.id,
            )
        except HTTPException:
            # Falha no Asaas (ex.: credenciais inválidas, 4xx) → não persiste assinatura
            db.rollback()
            raise
        except Exception as exc:
            # Falha de rede/timeout: NÃO persiste assinatura local órfã sem asaas_subscription_id.
            # Criar assinatura sem ID Asaas geraria débitos não rastreáveis no provider.
            # O cliente deve tentar novamente; o backend retornará 502.
            db.rollback()
            logger.exception(
                "create_subscription: falha de rede ao criar assinatura no Asaas; "
                "assinatura NÃO criada localmente para evitar registro órfão. error=%s", exc,
            )
            raise HTTPException(
                status_code=502,
                detail="Serviço de pagamento temporariamente indisponível. Tente novamente em instantes.",
            ) from exc

    if asaas_sub_id:
        subscription.asaas_subscription_id = asaas_sub_id

    db.commit()
    db.refresh(subscription)
    return subscription


def cancel_subscription(db: Session, tenant_id: str, tutor_id: str) -> TutorSubscription:
    """Versão síncrona (legada) — usada pelos testes existentes.

    Não cancela no Asaas. Para integração Asaas use cancel_subscription_async.
    """
    subscription = get_active_subscription(db, tenant_id, tutor_id)
    if not subscription:
        raise HTTPException(status_code=404, detail="Nenhuma assinatura ativa para cancelar.")
    now = datetime.utcnow()
    subscription.status = SUBSCRIPTION_CANCELLED
    subscription.cancelled_at = now
    subscription.updated_at = now
    db.add(subscription)
    db.commit()
    db.refresh(subscription)
    return subscription


async def cancel_subscription_async(db: Session, tenant_id: str, tutor_id: str) -> TutorSubscription:
    """Versão assíncrona: cancela subscription no Asaas + marca local como cancelled."""
    subscription = get_active_subscription(db, tenant_id, tutor_id)
    if not subscription:
        raise HTTPException(status_code=404, detail="Nenhuma assinatura ativa para cancelar.")

    # Cancela no Asaas (idempotente — 404 remoto é ignorado)
    if subscription.asaas_subscription_id and cancel_asaas_subscription is not None:
        await cancel_asaas_subscription(subscription.asaas_subscription_id)

    now = datetime.utcnow()
    subscription.status = SUBSCRIPTION_CANCELLED
    subscription.cancelled_at = now
    subscription.updated_at = now
    db.add(subscription)
    db.commit()
    db.refresh(subscription)
    return subscription


def plan_name_for(db: Session, subscription: TutorSubscription | None) -> str | None:
    if not subscription:
        return None
    plan = db.get(RecurringPlan, subscription.plan_id)
    return plan.name if plan else None


def reset_credits_if_renewal(db: Session, subscription: TutorSubscription) -> bool:
    """Reabastece os créditos na renovação mensal paga, idempotente e thread-safe.

    Só reseta quando o período atual já venceu (current_period_end < now). Isso evita
    reabastecer na 1ª cobrança (período recém-criado) e em reentrega do mesmo evento.
    Releitura com with_for_update serializa duas entregas concorrentes (a 2ª vê o
    período já avançado e desiste). Avança o período. Não commita. Retorna True se resetou.
    """
    now = datetime.utcnow()
    locked = (
        db.query(TutorSubscription)
        .filter(TutorSubscription.id == subscription.id)
        .with_for_update()
        .first()
    )
    if locked is None:
        return False
    end = locked.current_period_end
    if end is None or end > now:
        return False
    plan = db.get(RecurringPlan, locked.plan_id)
    interval = plan.interval if plan else "monthly"
    locked.credits_remaining = locked.walks_per_cycle
    locked.current_period_start = now
    locked.current_period_end = _period_end(now, interval)
    locked.updated_at = now
    db.add(locked)
    return True


def refund_credit_for_walk(db: Session, walk) -> bool:
    """Estorna 1 crédito quando um passeio coberto por assinatura é cancelado/deletado.

    Só estorna se: subscription_id != None, ainda não estornado (credit_refunded False),
    a assinatura existe e está ativa, e o passeio pertence ao ciclo atual
    (walk.created_at >= current_period_start). Idempotente. Não commita.
    """
    if not getattr(walk, "subscription_id", None) or getattr(walk, "credit_refunded", False):
        return False
    sub = db.get(TutorSubscription, walk.subscription_id)
    if sub is None or sub.status != SUBSCRIPTION_ACTIVE:
        return False
    period_start = sub.current_period_start
    walk_created = getattr(walk, "created_at", None)
    if period_start and walk_created and walk_created < period_start:
        return False
    sub.credits_remaining += 1
    sub.updated_at = datetime.utcnow()
    walk.credit_refunded = True
    db.add(sub); db.add(walk)
    return True
