"""Serviço de assinaturas nativas do Asaas (Fase 7 $-2).

Cria e cancela subscriptions via API nativa `POST /subscriptions` do Asaas.
Usa a mesma configuração de ambiente de app/routes/payments.py (_get_asaas_config).

Mapeamento de interval → cycle do Asaas:
  monthly   → MONTHLY
  weekly    → WEEKLY
  biweekly  → BIWEEKLY
  quarterly → QUARTERLY
  yearly    → YEARLY

Regra de criação sem zumbi: a subscription local só é gravada APÓS sucesso
remoto no Asaas. Se o Asaas retornar erro, levanta HTTPException 502 PT
e nada é persistido (a camada de serviço faz o commit só após este call).
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

import httpx
from fastapi import HTTPException

logger = logging.getLogger("aumigao.asaas_subscription_service")

# Mapeamento interval → billingCycle do Asaas
INTERVAL_TO_ASAAS_CYCLE: dict[str, str] = {
    "weekly": "WEEKLY",
    "biweekly": "BIWEEKLY",
    "monthly": "MONTHLY",
    "quarterly": "QUARTERLY",
    "yearly": "YEARLY",
}

DEFAULT_CYCLE = "MONTHLY"


def _get_config() -> dict:
    """Obtém configuração do Asaas via payments module.

    Retorna None quando o payments module não está disponível (ex.: testes
    que não montam o módulo completo) — o caller deve tratar.
    """
    try:
        from app.routes.payments import _get_asaas_config
        return _get_asaas_config()
    except Exception as exc:
        logger.warning("asaas_subscription_service: _get_asaas_config indisponível. error=%s", exc)
        return None


def _headers(api_key: str, *, is_live: bool) -> dict:
    mode = "live" if is_live else "sandbox"
    return {
        "access_token": api_key,
        "Content-Type": "application/json",
        "User-Agent": f"Aumigao Subscriptions {mode.capitalize()}",
    }


async def create_asaas_subscription(
    *,
    customer_id: str,
    value: float,
    interval: str,
    tutor_subscription_id: str,
    next_due_date: date | None = None,
) -> str | None:
    """Cria subscription no Asaas e retorna o ID remoto.

    Retorna None quando o Asaas não está configurado (modo internal_mock).
    Levanta HTTPException 502 em caso de falha remota.
    """
    cfg = _get_config()
    if cfg is None:
        return None

    api_key = cfg["api_key"]
    if not api_key:
        return None

    base_url = cfg["base_url"]
    is_live = cfg["is_live"]
    cycle = INTERVAL_TO_ASAAS_CYCLE.get(interval, DEFAULT_CYCLE)

    due = next_due_date or (date.today() + timedelta(days=1))

    payload = {
        "customer": customer_id,
        "billingType": "UNDEFINED" if not is_live else "BOLETO",
        "value": value,
        "nextDueDate": str(due),
        "cycle": cycle,
        "externalReference": f"sub:{tutor_subscription_id}",
        "description": "Assinatura recorrente Aumigao",
    }

    try:
        async with httpx.AsyncClient(
            base_url=base_url,
            headers=_headers(api_key, is_live=is_live),
            timeout=20,
        ) as client:
            response = await client.post("/subscriptions", json=payload)
            if response.status_code >= 400:
                try:
                    err = response.json()
                except Exception:
                    err = {"raw": response.text}
                msg = (err.get("errors") or [{}])[0].get("description") or err.get("description") or "Erro desconhecido"
                logger.error(
                    "asaas_subscription create failed status=%s body=%s",
                    response.status_code, err,
                )
                raise HTTPException(
                    status_code=502,
                    detail=f"Falha ao criar assinatura no gateway de pagamento: {msg}",
                )
            data = response.json()
            sub_id = data.get("id")
            logger.info(
                "asaas_subscription created id=%s tutor_sub=%s cycle=%s",
                sub_id, tutor_subscription_id, cycle,
            )
            return sub_id
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("asaas_subscription create network error: %s", exc)
        raise HTTPException(
            status_code=502,
            detail="Gateway de pagamento indisponível ao criar assinatura. Tente novamente em instantes.",
        )


async def cancel_asaas_subscription(asaas_subscription_id: str) -> None:
    """Cancela subscription no Asaas (DELETE /subscriptions/{id}).

    Silencioso se já cancelada (404 do Asaas é ignorado).
    Levanta HTTPException 502 em outros erros de rede/API.
    """
    cfg = _get_config()
    if cfg is None or not asaas_subscription_id:
        return

    api_key = cfg["api_key"]
    if not api_key:
        return

    base_url = cfg["base_url"]
    is_live = cfg["is_live"]

    try:
        async with httpx.AsyncClient(
            base_url=base_url,
            headers=_headers(api_key, is_live=is_live),
            timeout=20,
        ) as client:
            response = await client.delete(f"/subscriptions/{asaas_subscription_id}")
            if response.status_code == 404:
                logger.info("asaas_subscription cancel: já inexistente id=%s", asaas_subscription_id)
                return
            if response.status_code >= 400:
                try:
                    err = response.json()
                except Exception:
                    err = {"raw": response.text}
                msg = (err.get("errors") or [{}])[0].get("description") or err.get("description") or "Erro desconhecido"
                logger.error(
                    "asaas_subscription cancel failed id=%s status=%s body=%s",
                    asaas_subscription_id, response.status_code, err,
                )
                raise HTTPException(
                    status_code=502,
                    detail=f"Falha ao cancelar assinatura no gateway de pagamento: {msg}",
                )
            logger.info("asaas_subscription cancelled id=%s", asaas_subscription_id)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("asaas_subscription cancel network error id=%s: %s", asaas_subscription_id, exc)
        raise HTTPException(
            status_code=502,
            detail="Gateway de pagamento indisponível ao cancelar assinatura. Tente novamente em instantes.",
        )
