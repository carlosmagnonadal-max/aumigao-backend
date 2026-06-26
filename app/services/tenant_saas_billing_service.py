"""Serviço de cobrança SaaS para tenants (Projeto B).

Task 4:
  - ensure_tenant_asaas_customer: garante que o tenant possui um customer_id
    no Asaas, criando-o se necessário. Idempotente: retorna o id existente sem
    tocar a rede. A validação de documento/e-mail ocorre ANTES de qualquer
    acesso à rede (testes offline passam).

Task 5:
  - start_subscription: cria assinatura SaaS local + remote no Asaas.
    Anti-zumbi: a subscription local só persiste após sucesso remoto.
    Idempotência parcial: cancela assinatura ativa anterior antes de criar nova.
  - cancel_subscription: cancela assinatura ativa do tenant (local + Asaas).
"""
from __future__ import annotations

import logging
from datetime import datetime

import httpx
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.tenant import Tenant
from app.models.tenant_saas_subscription import (
    TenantSaasSubscription,
    SAAS_ACTIVE,
    SAAS_CANCELLED,
    SAAS_OVERDUE,
)
from app.services.asaas_subscription_service import (
    create_asaas_subscription as create_asaas_subscription_native,
    cancel_asaas_subscription,
)
from app.services.tenant_saas_pricing import resolve_saas_price

logger = logging.getLogger("aumigao.tenant_saas_billing_service")


# ─────────────────────────────────────────────── helpers ──────────────────────

def get_active_saas_subscription(
    db: Session, tenant_id: str
) -> TenantSaasSubscription | None:
    """Retorna a assinatura SaaS ativa mais recente do tenant, ou None."""
    return (
        db.query(TenantSaasSubscription)
        .filter(
            TenantSaasSubscription.tenant_id == tenant_id,
            TenantSaasSubscription.status == SAAS_ACTIVE,
        )
        .order_by(TenantSaasSubscription.created_at.desc())
        .first()
    )


def _period_end_month(now: datetime) -> datetime:
    """Retorna data de fim do período mensal (now + 1 mês)."""
    from app.services.recurring_plan_service import _period_end
    return _period_end(now, "monthly")


# ─────────────────────────────────── ensure_tenant_asaas_customer ─────────────

async def ensure_tenant_asaas_customer(db: Session, tenant: Tenant) -> str:
    """Garante que o tenant possui um customer_id no Asaas.

    1. Idempotência: se tenant.asaas_customer_id já está preenchido, retorna-o
       imediatamente sem qualquer chamada de rede.
    2. Validação offline: valida documento (CPF 11 dígitos ou CNPJ 14 dígitos)
       e e-mail ANTES de tentar configuração de gateway ou rede. Levanta
       HTTPException 400 quando inválidos (testes offline passam sem mock).
    3. Configuração: checa se o gateway está configurado (api_key presente);
       levanta HTTPException 502 se não estiver.
    4. Criação: faz POST /customers no Asaas, persiste o id recebido com
       commit próprio (idempotência real — retry não recria o customer).

    Returns:
        str: customer_id do Asaas (pré-existente ou recém-criado).

    Raises:
        HTTPException 400: documento inválido ou e-mail ausente.
        HTTPException 502: gateway não configurado ou erro remoto.
    """
    # ── 1. Idempotência: customer já registrado ──────────────────────────────
    if tenant.asaas_customer_id:
        logger.debug(
            "ensure_tenant_asaas_customer: tenant=%s já tem customer_id=%s",
            tenant.id, tenant.asaas_customer_id,
        )
        return tenant.asaas_customer_id

    # ── 2. Validação offline (antes de tocar a rede) ─────────────────────────
    doc = "".join(c for c in (tenant.document_number or "") if c.isdigit())
    if len(doc) not in (11, 14):
        logger.warning(
            "ensure_tenant_asaas_customer: tenant=%s documento inválido (len=%d)",
            tenant.id, len(doc),
        )
        raise HTTPException(
            status_code=400,
            detail="Complete CNPJ e e-mail do tenant antes de cobrar.",
        )
    if not tenant.contact_email:
        logger.warning(
            "ensure_tenant_asaas_customer: tenant=%s sem contact_email", tenant.id,
        )
        raise HTTPException(
            status_code=400,
            detail="Complete CNPJ e e-mail do tenant antes de cobrar.",
        )

    # ── 3. Configuração do gateway ────────────────────────────────────────────
    # Import local para não criar dependência circular no nível de módulo.
    from app.routes.payments import _get_asaas_config  # noqa: PLC0415
    try:
        cfg = _get_asaas_config()
    except HTTPException:
        cfg = None

    if not cfg or not cfg.get("api_key"):
        logger.error(
            "ensure_tenant_asaas_customer: tenant=%s gateway não configurado", tenant.id,
        )
        raise HTTPException(
            status_code=502,
            detail="Gateway de pagamento não configurado.",
        )

    # ── 4. Criação do customer no Asaas ──────────────────────────────────────
    base_url: str = cfg["base_url"]
    api_key: str = cfg["api_key"]
    is_live: bool = cfg["is_live"]
    mode_label = "live" if is_live else "sandbox"

    payload = {
        "name": tenant.legal_name or tenant.name,
        "email": tenant.contact_email,
        "cpfCnpj": doc,
        "externalReference": f"tenant:{tenant.id}",
        "notificationDisabled": False,
    }

    logger.info(
        "ensure_tenant_asaas_customer: criando customer Asaas para tenant=%s mode=%s",
        tenant.id, mode_label,
    )

    try:
        async with httpx.AsyncClient(
            base_url=base_url,
            headers={
                "access_token": api_key,
                "Content-Type": "application/json",
                "User-Agent": f"Aumigao SaaS Billing {mode_label.capitalize()}",
            },
            timeout=20,
        ) as client:
            response = await client.post("/customers", json=payload)
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
                logger.error(
                    "ensure_tenant_asaas_customer: Asaas retornou status=%s body=%s tenant=%s",
                    response.status_code, err, tenant.id,
                )
                raise HTTPException(
                    status_code=502,
                    detail=f"Falha ao criar customer no gateway de pagamento: {msg}",
                )
            data = response.json()
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception(
            "ensure_tenant_asaas_customer: erro de rede para tenant=%s: %s",
            tenant.id, exc,
        )
        raise HTTPException(
            status_code=502,
            detail="Gateway de pagamento indisponível ao criar customer. Tente novamente em instantes.",
        )

    customer_id: str = data["id"]

    # ── 5. Persistência com commit próprio (idempotência real) ────────────────
    # Commit aqui garante que, se houver retry, o customer já registrado
    # será encontrado no passo 1 sem recriar no Asaas.
    tenant.asaas_customer_id = customer_id
    db.add(tenant)
    db.commit()

    logger.info(
        "ensure_tenant_asaas_customer: customer_id=%s persistido para tenant=%s",
        customer_id, tenant.id,
    )
    return customer_id


# ──────────────────────────────────────── start_subscription ──────────────────

async def start_subscription(
    db: Session,
    tenant: Tenant,
    price: float | None = None,
) -> TenantSaasSubscription:
    """Cria assinatura SaaS para o tenant.

    Ordem de operações (anti-zumbi):
      1. Resolve preço canônico.
      2. ensure_tenant_asaas_customer — commita o customer isoladamente.
         Isso garante que o commit subsequente NÃO carrega dados não-desejados.
      3. Cancela assinatura ativa anterior (idempotente no Asaas).
      4. Cria objeto TenantSaasSubscription local + db.flush() (gera id, mas
         NÃO commita — a sessão ainda está suja).
      5. Chama Asaas para criar a subscription remota.
         Em falha → db.rollback() remove o flush; NADA persiste (zero-zumbi).
      6. Sucesso → persiste asaas_subscription_id e commit.

    Raises:
        HTTPException 400/502: validação ou gateway.
        HTTPException 409: race condition (sub ativa duplicada).
        Qualquer exceção do Asaas: propagada; subscription local não persiste.
    """
    # ── 1. Resolve preço ──────────────────────────────────────────────────────
    value = resolve_saas_price(tenant.plan, price)

    # ── 2. Garante customer no Asaas (commit próprio — isola o flush do sub) ──
    # IMPORTANTE: ensure_tenant_asaas_customer faz db.commit() internamente.
    # Chamá-la ANTES de adicionar o TenantSaasSubscription à sessão garante
    # que esse commit não persista o sub prematuramente. Após este ponto,
    # a sessão está limpa (nenhum objeto pendente).
    customer_id = await ensure_tenant_asaas_customer(db, tenant)

    # ── 3. Cancela assinatura ativa OU inadimplente anterior ─────────────────
    existing = (
        db.query(TenantSaasSubscription)
        .filter(
            TenantSaasSubscription.tenant_id == tenant.id,
            TenantSaasSubscription.status.in_([SAAS_ACTIVE, SAAS_OVERDUE]),
        )
        .order_by(TenantSaasSubscription.created_at.desc())
        .first()
    )
    if existing:
        if existing.asaas_subscription_id:
            await cancel_asaas_subscription(existing.asaas_subscription_id)
        existing.status = SAAS_CANCELLED
        db.add(existing)
        # Não commitamos aqui — vamos commitar tudo junto no passo 6.

    # ── 4. Cria objeto local + flush (gera id; NÃO commita) ───────────────────
    now = datetime.utcnow()
    sub = TenantSaasSubscription(
        tenant_id=tenant.id,
        plan=tenant.plan,
        price=value,
        status=SAAS_ACTIVE,
        current_period_start=now,
        current_period_end=_period_end_month(now),
    )
    db.add(sub)
    db.flush()  # gera sub.id sem commit

    # ── 5. Cria subscription no Asaas (se falhar → rollback, sem zumbi) ───────
    try:
        asaas_sub_id = await create_asaas_subscription_native(
            customer_id=customer_id,
            value=float(value),
            interval="monthly",
            external_reference=f"tenant_sub:{sub.id}",
        )
    except Exception:
        db.rollback()  # descarta o flush do sub (e o pending de existing)
        raise

    # ── M2: guarda contra id falsy (gateway não configurado retornou None) ────
    if not asaas_sub_id:
        db.rollback()
        raise HTTPException(
            status_code=502,
            detail="Gateway de pagamento não retornou a assinatura. Tente novamente.",
        )

    sub.asaas_subscription_id = asaas_sub_id

    # ── 6. Commit final ───────────────────────────────────────────────────────
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="Já existe assinatura ativa para este tenant.",
        )

    db.refresh(sub)
    logger.info(
        "start_subscription: tenant=%s plan=%s asaas_sub=%s",
        tenant.id, tenant.plan, asaas_sub_id,
    )
    return sub


# ──────────────────────────────────────── cancel_subscription ─────────────────

async def cancel_subscription(
    db: Session,
    tenant: Tenant,
) -> TenantSaasSubscription | None:
    """Cancela a assinatura SaaS ativa do tenant.

    Idempotente: retorna None se não há assinatura ativa.
    Cancela no Asaas antes de marcar localmente (falha no Asaas = não cancela local).
    """
    sub = get_active_saas_subscription(db, tenant.id)
    if sub is None:
        logger.info("cancel_subscription: tenant=%s sem assinatura ativa", tenant.id)
        return None

    if sub.asaas_subscription_id:
        await cancel_asaas_subscription(sub.asaas_subscription_id)

    sub.status = SAAS_CANCELLED
    db.commit()

    logger.info(
        "cancel_subscription: tenant=%s sub=%s cancelada", tenant.id, sub.id,
    )
    return sub


# ──────────────────────────────────────── sweep_overdue_tenants ───────────────

def sweep_overdue_tenants(db: Session, now: datetime | None = None, grace_days: int = 7) -> int:
    """Suspende tenants com assinatura SaaS vencida há mais de grace_days.

    Não toca tenants já suspensos/cancelados/pausados. Marca suspended_reason='billing'.
    Não commita (o caller commita). Retorna a contagem suspensa.
    """
    from datetime import timedelta
    cutoff = (now or datetime.utcnow()) - timedelta(days=grace_days)
    overdue = (
        db.query(TenantSaasSubscription)
        .filter(
            TenantSaasSubscription.status == SAAS_OVERDUE,
            TenantSaasSubscription.overdue_since.isnot(None),
            TenantSaasSubscription.overdue_since < cutoff,
        )
        .all()
    )
    n = 0
    for sub in overdue:
        tenant = db.get(Tenant, sub.tenant_id)
        if tenant and tenant.status not in ("suspended", "cancelled", "paused"):
            tenant.status = "suspended"
            tenant.suspended_reason = "billing"
            db.add(tenant)
            n += 1
    logger.info("sweep_overdue_tenants: %d tenant(s) suspensos (grace_days=%d)", n, grace_days)
    return n
