"""Serviço de emissão de NFS-e via Asaas.

Regras de ouro:
- Completamente dormente quando NFS_E_ENABLED=false (padrão).
- Emissão é BEST-EFFORT: falhas NUNCA propagam exceção para o caller.
- Idempotente por asaas_payment_id: nunca emite 2x para o mesmo pagamento.
"""
from __future__ import annotations

import logging
from datetime import datetime
from uuid import uuid4

import httpx

from app.models.nfse import (
    Nfse,
    NFSE_SCHEDULED,
    NFSE_SYNCHRONIZED,
    NFSE_AUTHORIZED,
    NFSE_CANCELED,
    NFSE_ERROR,
)
from app.services.nfse_config import (
    nfse_enabled,
    get_municipal_service_code,
    get_iss_rate,
    get_service_description,
    get_deductions,
)

logger = logging.getLogger("aumigao.nfse_service")

# Statuses que não bloqueam uma re-emissão (ex.: falha anterior pode ser retentada).
_TERMINAL_FOR_REISSUE = {NFSE_SCHEDULED, NFSE_SYNCHRONIZED, NFSE_AUTHORIZED}


def _build_invoice_payload(
    *,
    asaas_payment_id: str,
    value: float,
    service_type: str,  # noqa: ARG001 — reservado para futura diferenciação por tipo
) -> dict:
    """Monta o body do POST /v3/invoices para emissão no Asaas.

    TODO: Os campos `taxes` e `municipalServiceCode` dependem de definição do
    contador (regime tributário, CNAE, alíquota ISS, deduções). Enquanto não
    forem definidos, o payload é enviado sem esses campos — o Asaas pode exigir
    `municipalServiceCode` dependendo da prefeitura configurada na conta.
    """
    payload: dict = {
        "payment": asaas_payment_id,
        "value": float(value),
        "serviceDescription": get_service_description(),
        "deductions": get_deductions(),
        "effectiveDate": datetime.utcnow().date().isoformat(),
    }

    mun_code = get_municipal_service_code()
    if mun_code:
        payload["municipalServiceCode"] = mun_code

    iss_rate = get_iss_rate()
    if iss_rate > 0:
        # TODO: confirmar estrutura exata de `taxes` com o contador e a
        # documentação da prefeitura no Asaas — o campo pode variar.
        payload["taxes"] = {"iss": iss_rate}

    return payload


async def _create_asaas_invoice(payload: dict) -> dict:
    """Faz POST /v3/invoices no Asaas e retorna o JSON de resposta.

    Segue exatamente o padrão de asaas_subscription_service:
    - base_url + header access_token via _get_asaas_config
    - timeout 20s
    - erro HTTP → levanta exceção (HTTPException 502)

    Função isolada para ser facilmente mockada nos testes.
    """
    from app.routes.payments import _get_asaas_config
    from fastapi import HTTPException

    cfg = _get_asaas_config()
    api_key = cfg["api_key"]
    base_url = cfg["base_url"]
    is_live = cfg["is_live"]
    mode = "live" if is_live else "sandbox"

    headers = {
        "access_token": api_key,
        "Content-Type": "application/json",
        "User-Agent": f"Aumigao NFS-e {mode.capitalize()}",
    }

    async with httpx.AsyncClient(base_url=base_url, headers=headers, timeout=20) as client:
        response = await client.post("/invoices", json=payload)
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
                "nfse_service._create_asaas_invoice failed status=%s body=%s",
                response.status_code,
                err,
            )
            raise HTTPException(
                status_code=502,
                detail=f"Falha ao emitir NFS-e no gateway: {msg}",
            )
        return response.json()


async def issue_nfse_for_saas_payment(
    db,
    *,
    tenant_id: str,
    asaas_payment_id: str,
    value: float,
    subscription_id: str | None = None,
) -> Nfse | None:
    """Emite NFS-e para um pagamento de mensalidade SaaS.

    Retorna None (noop) quando a flag NFS_E_ENABLED está desligada.
    Retorna a Nfse existente (sem nova chamada HTTP) quando já foi emitida com sucesso.
    Em falha de API: persiste registro de erro e retorna a Nfse com status=error.
    NUNCA relança exceção — falha de NFS-e jamais pode quebrar o processamento de pagamento.
    """
    if not nfse_enabled():
        return None

    # ---- Idempotência --------------------------------------------------------
    existing = (
        db.query(Nfse)
        .filter(Nfse.asaas_payment_id == asaas_payment_id)
        .first()
    )
    if existing and existing.status not in (NFSE_ERROR, NFSE_CANCELED):
        logger.info(
            "issue_nfse_for_saas_payment: já existe nfse para asaas_payment_id=%s status=%s",
            asaas_payment_id,
            existing.status,
        )
        return existing

    # ---- Emissão -------------------------------------------------------------
    payload = _build_invoice_payload(
        asaas_payment_id=asaas_payment_id,
        value=value,
        service_type="saas",
    )

    try:
        data = await _create_asaas_invoice(payload)
        asaas_invoice_id = data.get("id")
        nfse = Nfse(
            id=str(uuid4()),
            tenant_id=tenant_id,
            asaas_payment_id=asaas_payment_id,
            subscription_id=subscription_id,
            asaas_invoice_id=asaas_invoice_id,
            service_type="saas",
            status=NFSE_SCHEDULED,
            value=value,
            external_reference=f"saas:{asaas_payment_id}",
        )
        db.add(nfse)
        db.commit()
        logger.info(
            "issue_nfse_for_saas_payment: nota agendada nfse_id=%s asaas_invoice_id=%s",
            nfse.id,
            asaas_invoice_id,
        )
        return nfse

    except Exception as exc:
        logger.exception(
            "issue_nfse_for_saas_payment: falha ao emitir NFS-e asaas_payment_id=%s: %s",
            asaas_payment_id,
            exc,
        )
        try:
            # Persiste erro para observabilidade — não relança
            err_nfse = Nfse(
                id=str(uuid4()),
                tenant_id=tenant_id,
                asaas_payment_id=asaas_payment_id,
                subscription_id=subscription_id,
                asaas_invoice_id=None,
                service_type="saas",
                status=NFSE_ERROR,
                value=value,
                error_message=str(exc),
                external_reference=f"saas:{asaas_payment_id}",
            )
            db.add(err_nfse)
            db.commit()
            return err_nfse
        except Exception:
            logger.exception(
                "issue_nfse_for_saas_payment: falha ao persistir erro de NFS-e asaas_payment_id=%s",
                asaas_payment_id,
            )
            try:
                db.rollback()
            except Exception:
                pass
            return None


def handle_nfse_webhook_event(db, event: str, invoice_data: dict) -> bool:
    """Processa eventos INVOICE_* do Asaas e atualiza a tabela nfse.

    Retorna True em todos os casos (consumiu o evento — mesmo noop).
    Idempotente: chamadas repetidas com os mesmos dados são seguras.
    """
    if not isinstance(event, str) or not event.startswith("INVOICE"):
        return True  # noop — não é evento de nota

    if not invoice_data:
        return True  # noop seguro

    asaas_invoice_id = invoice_data.get("id")
    if not asaas_invoice_id:
        return True

    nfse = (
        db.query(Nfse)
        .filter(Nfse.asaas_invoice_id == asaas_invoice_id)
        .first()
    )
    if nfse is None:
        logger.info(
            "handle_nfse_webhook_event: asaas_invoice_id=%s não encontrado localmente — noop",
            asaas_invoice_id,
        )
        return True

    if event == "INVOICE_AUTHORIZED":
        nfse.status = NFSE_AUTHORIZED
        nfse.nfse_number = invoice_data.get("number")
        nfse.pdf_url = invoice_data.get("pdfUrl")
        nfse.xml_url = invoice_data.get("xmlUrl")
        nfse.validation_code = invoice_data.get("validationCode")

    elif event == "INVOICE_CANCELED":
        nfse.status = NFSE_CANCELED

    elif event == "INVOICE_ERROR":
        nfse.status = NFSE_ERROR
        err = invoice_data.get("error")
        nfse.error_message = str(err) if err is not None else "Erro desconhecido"

    elif event == "INVOICE_SYNCHRONIZED":
        nfse.status = NFSE_SYNCHRONIZED

    elif event == "INVOICE_CREATED":
        # Nota criada no Asaas mas ainda não sincronizada — noop (status já é scheduled)
        pass

    else:
        # Demais INVOICE_* (ex.: INVOICE_UPDATED) — noop seguro
        logger.debug("handle_nfse_webhook_event: evento %s sem tratamento específico — noop", event)

    db.add(nfse)
    db.commit()
    return True
