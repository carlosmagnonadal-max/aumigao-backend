"""Serviço de cobrança SaaS para tenants (Projeto B — Task 4).

Responsabilidades deste módulo (Task 4):
  - ensure_tenant_asaas_customer: garante que o tenant possui um customer_id
    no Asaas, criando-o se necessário. Idempotente: retorna o id existente sem
    tocar a rede. A validação de documento/e-mail ocorre ANTES de qualquer
    acesso à rede (testes offline passam).

Tasks seguintes (Task 5) adicionarão start_tenant_saas_subscription e
cancel_tenant_saas_subscription aqui.
"""
from __future__ import annotations

import logging

import httpx
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.tenant import Tenant

logger = logging.getLogger("aumigao.tenant_saas_billing_service")


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
