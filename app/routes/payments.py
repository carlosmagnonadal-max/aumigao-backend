import os
import asyncio
import json
import secrets
import logging
from contextvars import ContextVar
from datetime import date, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from dotenv import load_dotenv

from app.core.database import get_db, get_global_db
from app.dependencies.auth import get_current_user
from app.models.notification import Notification
from app.models.payment import Payment
from app.models.fiscal import REVENUE_WALK_COMMISSION, REVENUE_SAAS_SUBSCRIPTION, REVENUE_TIP
from app.models.tenant import Tenant
from app.models.tenant_saas_subscription import TenantSaasSubscription, SAAS_ACTIVE, SAAS_OVERDUE
from app.models.user import User
from app.models.walk import Walk
from app.schemas.payment import PaymentCreate, PaymentQuoteResponse, PaymentResponse
from app.services.payment_split_service import build_payment_split, build_quote, walker_percent_from_split

router = APIRouter(prefix="/payments", tags=["payments"])
logger = logging.getLogger("app.routes.payments")

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

# ---------------------------------------------------------------------------
# Configuração de modo de pagamento
#
# PAYMENT_MODE aceita:
#   "asaas_sandbox"  (default) — sandbox Asaas, sem cobrança real.
#   "asaas_live"               — produção Asaas; ativa quando as envs abaixo
#                                estiverem configuradas no Railway.
#
# Envs necessárias para asaas_live:
#   ASAAS_LIVE_API_KEY    — chave live da conta Asaas (obrigatória no modo live)
#   ASAAS_LIVE_BASE_URL   — override da URL base (opcional; default api.asaas.com/v3)
# ---------------------------------------------------------------------------
PAYMENT_MODE = os.getenv("PAYMENT_MODE", "asaas_sandbox")

# --- Sandbox ---
ASAAS_SANDBOX_BASE_URL = os.getenv("ASAAS_SANDBOX_BASE_URL", "https://api-sandbox.asaas.com/v3").rstrip("/")
ASAAS_SANDBOX_API_KEY = os.getenv("ASAAS_SANDBOX_API_KEY") or os.getenv("ASAAS_API_KEY")
ASAAS_SANDBOX_DEFAULT_CPF_CNPJ = os.getenv("ASAAS_SANDBOX_DEFAULT_CPF_CNPJ", "24971563792")

# --- Live (dormente por default — só ativa com PAYMENT_MODE=asaas_live) ---
ASAAS_LIVE_BASE_URL = os.getenv("ASAAS_LIVE_BASE_URL", "https://api.asaas.com/v3").rstrip("/")
ASAAS_LIVE_API_KEY = os.getenv("ASAAS_LIVE_API_KEY")

# SENSITIVE_KEYS and sanitize_for_log are now canonical in app.core.log_masking (DRY).
# Kept as re-exports here for backwards compatibility with any direct imports.
from app.core.log_masking import SENSITIVE_KEYS, sanitize_for_log  # noqa: E402

# ---------------------------------------------------------------------------
# ContextVars — async-safe, sem alterar assinatura pública de
# create_asaas_payment (que é monkeypatchada nos testes existentes).
# O router seta os valores antes de chamar create_asaas_payment;
# a função lê os valores dentro da mesma tarefa asyncio.
# ---------------------------------------------------------------------------
_split_config_ctx: ContextVar[dict | None] = ContextVar("_split_config_ctx", default=None)

# CPF do TutorProfile (apenas dígitos, 11 chars). None quando não disponível
# (sandbox usa fallback; live rejeita com 400 dentro de create_asaas_customer).
_tutor_cpf_ctx: ContextVar[str | None] = ContextVar("_tutor_cpf_ctx", default=None)

# ---------------------------------------------------------------------------
# Mapeamentos de status
#
# Os identificadores internos (ex.: "pagamento_confirmado_sandbox") são usados
# pelo admin-web e pelo app mobile — NÃO renomear. O sufixo "_sandbox" é apenas
# histórico; esses mesmos status são reutilizados no modo live para não exigir
# migração de dados nem atualização dos clients.
# ---------------------------------------------------------------------------
STATUS_BY_ASAAS_STATUS = {
    "PENDING": "pagamento_sandbox_criado",
    "RECEIVED": "pagamento_confirmado_sandbox",
    "CONFIRMED": "pagamento_confirmado_sandbox",
    "OVERDUE": "falha_pagamento",
    "REFUNDED": "falha_pagamento",
    "RECEIVED_IN_CASH": "pagamento_confirmado_sandbox",
    "REFUND_REQUESTED": "falha_pagamento",
    "CHARGEBACK_REQUESTED": "falha_pagamento",
    "CHARGEBACK_DISPUTE": "falha_pagamento",
    "AWAITING_CHARGEBACK_REVERSAL": "aguardando_pagamento",
    "DUNNING_REQUESTED": "aguardando_pagamento",
    "DUNNING_RECEIVED": "pagamento_confirmado_sandbox",
    "AWAITING_RISK_ANALYSIS": "aguardando_pagamento",
}

STATUS_BY_WEBHOOK_EVENT = {
    "PAYMENT_CREATED": "pagamento_sandbox_criado",
    "PAYMENT_UPDATED": "aguardando_pagamento",
    "PAYMENT_CONFIRMED": "pagamento_confirmado_sandbox",
    "PAYMENT_RECEIVED": "pagamento_confirmado_sandbox",
    "PAYMENT_OVERDUE": "falha_pagamento",
    "PAYMENT_DELETED": "falha_pagamento",
    "PAYMENT_RESTORED": "aguardando_pagamento",
    "PAYMENT_REFUNDED": "falha_pagamento",
    "PAYMENT_CHARGEBACK_REQUESTED": "falha_pagamento",
    "PAYMENT_CHARGEBACK_DISPUTE": "falha_pagamento",
    "PAYMENT_AWAITING_CHARGEBACK_REVERSAL": "aguardando_pagamento",
    "PAYMENT_DUNNING_RECEIVED": "pagamento_confirmado_sandbox",
    "PAYMENT_DUNNING_REQUESTED": "aguardando_pagamento",
    "PAYMENT_BANK_SLIP_VIEWED": "aguardando_pagamento",
    "PAYMENT_CHECKOUT_VIEWED": "aguardando_pagamento",
}


# ---------------------------------------------------------------------------
# Helpers de configuração por modo
# ---------------------------------------------------------------------------

def _get_asaas_config() -> dict:
    """Retorna base_url, api_key e is_live de acordo com PAYMENT_MODE.

    Levanta HTTPException 503 se o modo live não tiver chave configurada.
    Levanta HTTPException 400 para modos desconhecidos.
    """
    if PAYMENT_MODE == "asaas_sandbox":
        return {
            "base_url": ASAAS_SANDBOX_BASE_URL,
            "api_key": ASAAS_SANDBOX_API_KEY,
            "is_live": False,
        }
    if PAYMENT_MODE == "asaas_live":
        if not ASAAS_LIVE_API_KEY:
            raise HTTPException(
                status_code=503,
                detail=(
                    "Pagamento em produção não configurado. "
                    "Configure ASAAS_LIVE_API_KEY no ambiente para ativar o modo live."
                ),
            )
        return {
            "base_url": ASAAS_LIVE_BASE_URL,
            "api_key": ASAAS_LIVE_API_KEY,
            "is_live": True,
        }
    raise HTTPException(
        status_code=400,
        detail=f"PAYMENT_MODE '{PAYMENT_MODE}' desconhecido. Use 'asaas_sandbox' ou 'asaas_live'.",
    )


def asaas_headers(api_key: str | None = None, *, mode: str | None = None) -> dict:
    """Retorna headers HTTP para chamadas ao Asaas.

    Se api_key não for passado, usa a chave do PAYMENT_MODE atual.
    Mantém compatibilidade retroativa: chamadas sem argumentos continuam
    funcionando como antes para o sandbox.
    """
    if api_key is None:
        cfg = _get_asaas_config()
        api_key = cfg["api_key"]
        effective_mode = "live" if cfg["is_live"] else "sandbox"
    else:
        effective_mode = mode or "sandbox"

    if not api_key:
        raise HTTPException(status_code=503, detail="ASAAS_SANDBOX_API_KEY nao configurada para o sandbox.")

    return {
        "access_token": api_key,
        "Content-Type": "application/json",
        "User-Agent": f"Aumigao Beta {effective_mode.capitalize()}",
    }


def parse_asaas_response(response: httpx.Response):
    try:
        return response.json()
    except ValueError:
        return {"raw": response.text}


def extract_asaas_error(data):
    errors = data.get("errors") if isinstance(data, dict) else None
    first_error = errors[0] if isinstance(errors, list) and errors else {}
    if not isinstance(first_error, dict):
        first_error = {}
    return {
        "code": first_error.get("code") or (data.get("code") if isinstance(data, dict) else None),
        "description": first_error.get("description")
        or first_error.get("message")
        or (data.get("description") if isinstance(data, dict) else None)
        or (data.get("message") if isinstance(data, dict) else None),
    }


def raise_asaas_error(step: str, response: httpx.Response, request_payload: dict | None = None):
    response_data = parse_asaas_response(response)
    asaas_error = extract_asaas_error(response_data)
    mode_label = "Live" if PAYMENT_MODE == "asaas_live" else "Sandbox"
    diagnostic = {
        "step": step,
        "status_http": response.status_code,
        "asaas_code": asaas_error["code"],
        "asaas_description": asaas_error["description"],
        "request_payload": sanitize_for_log(request_payload or {}),
        "asaas_response": sanitize_for_log(response_data),
    }
    logger.error("Asaas %s error: %s", mode_label, diagnostic)
    raise HTTPException(
        status_code=502,
        detail={
            "message": f"Falha ao processar pagamento em {step}. Tente novamente ou entre em contato com o suporte.",
            "asaas_code": asaas_error["code"],
        },
    )


def normalize_method(method: str, *, is_live: bool = False) -> str:
    """Normaliza o método de pagamento para o billingType do Asaas.

    No sandbox, cartão é sempre UNDEFINED (Asaas sandbox não processa cartão real).
    No modo live, cartão usa CREDIT_CARD para acionar o checkout hospedado.
    """
    normalized = (method or "pix").strip().lower()
    is_card = normalized in {"card", "credit_card", "cartao", "cartão"}
    if is_card:
        return "CREDIT_CARD" if is_live else "UNDEFINED"
    return "PIX"


def normalize_payment_status(provider_status: str | None):
    if not provider_status:
        return "pagamento_sandbox_criado"
    return STATUS_BY_ASAAS_STATUS.get(provider_status.upper(), "aguardando_pagamento")


def payment_response(payment: Payment, **extra):
    return {
        "id": payment.id,
        "tutor_id": payment.tutor_id,
        "walk_id": payment.walk_id,
        "amount": payment.amount,
        "provider": payment.provider,
        "method": extra.get("method") or "pix",
        "status": payment.status,
        "provider_payment_id": payment.provider_payment_id,
        "provider_status": extra.get("provider_status"),
        "invoice_url": extra.get("invoice_url") or payment.invoice_url,
        "pix_qr_code": extra.get("pix_qr_code"),
        "pix_copy_paste": extra.get("pix_copy_paste"),
        "pix_expiration_date": extra.get("pix_expiration_date"),
        # sandbox_message: quando o caller passa explicitamente (create), vale o que veio
        # (None no live). Quando NÃO passa (ex: get_payment), o default de sandbox só se
        # aplica a pagamento não-live — cobrança REAL nunca exibe aviso de sandbox
        # (o app mostra esse texto ao tutor; em asaas_live seria falso e enganoso).
        "sandbox_message": (
            extra["sandbox_message"]
            if "sandbox_message" in extra
            else (
                None
                if payment.provider == "asaas_live"
                else "Ambiente Sandbox: nenhuma cobranca real sera realizada."
            )
        ),
        "commission_percent": payment.commission_percent,
        "platform_amount": payment.platform_amount,
        "walker_amount": payment.walker_amount,
        "created_at": payment.created_at,
    }


async def create_asaas_customer(
    client: httpx.AsyncClient,
    user: User,
    *,
    is_live: bool = False,
    tutor_cpf: str | None = None,
):
    """Cria ou busca um customer no Asaas para o usuário.

    tutor_cpf: CPF do TutorProfile (apenas dígitos, 11 chars) pré-carregado pelo
    caller para evitar acesso a banco dentro da coroutine.

    Lógica de CPF:
    - Sandbox: usa tutor_cpf quando disponível, senão usa ASAAS_SANDBOX_DEFAULT_CPF_CNPJ.
    - Live: exige tutor_cpf válido; sem ele lança HTTPException 400.
    """
    if is_live:
        if not tutor_cpf or len(tutor_cpf) != 11:
            raise HTTPException(
                status_code=400,
                detail="Informe seu CPF no perfil para concluir o pagamento.",
            )
        cpf_cnpj = tutor_cpf
    else:
        cpf_cnpj = tutor_cpf if (tutor_cpf and len(tutor_cpf) == 11) else ASAAS_SANDBOX_DEFAULT_CPF_CNPJ

    payload = {
        "name": user.full_name or user.email,
        "email": user.email,
        "cpfCnpj": cpf_cnpj,
        "externalReference": user.id,
        "notificationDisabled": True,
    }
    mode_label = "Live" if is_live else "Sandbox"
    _log_payload = {**sanitize_for_log(payload), "email": "***"}
    logger.info("Asaas %s request customers payload=%s", mode_label, _log_payload)
    response = await client.post("/customers", json=payload)
    if response.status_code >= 400:
        raise_asaas_error("customers.create", response, payload)
    data = response.json()
    logger.info(
        "Asaas %s response customers status_http=%s customer_id=%s",
        mode_label, response.status_code, data.get("id"),
    )
    return data["id"]


async def create_asaas_payment(payload: PaymentCreate, user: User):
    """Cria pagamento no Asaas de acordo com o PAYMENT_MODE atual.

    Assinatura estável (payload, user) — compatível com os mocks de teste existentes.
    A lógica de split real é injetada por _build_split_config_for_payment antes
    da chamada e aplicada via _apply_asaas_split_to_payload (função auxiliar pura).
    """
    cfg = _get_asaas_config()
    base_url = cfg["base_url"]
    api_key = cfg["api_key"]
    is_live = cfg["is_live"]

    billing_type = normalize_method(payload.method, is_live=is_live)
    mode_label = "Live" if is_live else "Sandbox"

    # split_config é injetado pelo router via _split_config_ctx antes de chamar
    # esta função — async-safe via ContextVar, sem alterar a assinatura pública.
    split_config = _split_config_ctx.get()

    # CPF do TutorProfile — pré-carregado via ContextVar (async-safe).
    # O router injeta _tutor_cpf_ctx antes de chamar create_asaas_payment;
    # sem injeção (mocks de teste antigos) o valor é None → sandbox usa default.
    tutor_cpf = _tutor_cpf_ctx.get()

    async with httpx.AsyncClient(
        base_url=base_url,
        headers=asaas_headers(api_key, mode="live" if is_live else "sandbox"),
        timeout=20,
    ) as client:
        customer_id = await create_asaas_customer(client, user, is_live=is_live, tutor_cpf=tutor_cpf)
        # Validade da cobrança:
        # - PIX: expira no MESMO DIA (dueDate = hoje) e o Asaas cancela o registro no
        #   vencimento (daysAfterDueDateToRegistrationCancellation: 0). Fecha a janela de
        #   "pagou tarde demais" no próprio gateway — o corte operacional (início−45min)
        #   ainda cancela a cobrança antes disso.
        # - Cartão: mantém dueDate D+1 (checkout hospedado precisa de janela mínima).
        payment_payload = {
            "customer": customer_id,
            "billingType": billing_type,
            "value": payload.amount,
            "dueDate": str(date.today() if billing_type == "PIX" else date.today() + timedelta(days=1)),
            "description": "Passeio Aumigao",
            "externalReference": payload.walk_id or str(uuid4()),
        }
        if billing_type == "PIX":
            payment_payload["daysAfterDueDateToRegistrationCancellation"] = 0

        # Split real ao walker (dormente — opt-in duplo: split_enabled + asaas_wallet_id + modo live)
        if is_live and split_config and split_config.get("wallet_id"):
            payment_payload["split"] = [
                {
                    "walletId": split_config["wallet_id"],
                    "percentualValue": split_config["percentual_value"],
                }
            ]
            logger.info(
                "Asaas Live split incluido wallet_id=%s percentual_value=%s",
                split_config["wallet_id"],
                split_config["percentual_value"],
            )

        logger.info("Asaas %s request payments payload=%s", mode_label, sanitize_for_log(payment_payload))
        response = await client.post("/payments", json=payment_payload)
        if response.status_code >= 400:
            raise_asaas_error("payments.create", response, payment_payload)

        payment_data = response.json()
        logger.info(
            "Asaas %s response payments status_http=%s payment_id=%s provider_status=%s billing_type=%s",
            mode_label,
            response.status_code,
            payment_data.get("id"),
            payment_data.get("status"),
            billing_type,
        )
        pix_data = {}
        if billing_type == "PIX":
            pix_response = None
            for attempt in range(1, 4):
                pix_response = await client.get(f"/payments/{payment_data['id']}/pixQrCode")
                if pix_response.status_code < 400:
                    break
                logger.warning(
                    "Asaas %s pix_qr_code retry attempt=%s status_http=%s payment_id=%s",
                    mode_label,
                    attempt,
                    pix_response.status_code,
                    payment_data.get("id"),
                )
                await asyncio.sleep(attempt)
            if pix_response is None or pix_response.status_code >= 400:
                raise_asaas_error(
                    "payments.pix_qr_code",
                    pix_response,
                    {"payment_id": payment_data.get("id"), "billingType": billing_type},
                )
            pix_data = pix_response.json()
            logger.info(
                "Asaas %s response pix_qr_code status_http=%s payment_id=%s has_payload=%s",
                mode_label,
                pix_response.status_code,
                payment_data.get("id"),
                bool(pix_data.get("payload")),
            )

        return payment_data, pix_data, billing_type


# ─────────────────────── mutações de cobrança no Asaas ────────────────────────
# Helpers reutilizados por: recriação de cobrança (item B), corte de 45min no
# scheduler (item C) e decisão do tutor (item E). Todos best-effort — o Asaas
# nunca deve travar o fluxo local de estado/dinheiro. Retornam bool de sucesso.

def _is_asaas_provider_charge(provider: str | None, provider_payment_id: str | None) -> bool:
    """True se a cobrança realmente existe no Asaas (provider asaas_* e id não-interno).

    Cobranças com id `internal-sandbox-*` são fallback local que NÃO existe no
    gateway — chamar DELETE/refund nelas geraria 404 desnecessário.
    """
    if (provider or "") not in {"asaas_live", "asaas_sandbox"}:
        return False
    pid = provider_payment_id or ""
    return bool(pid) and not pid.startswith("internal-sandbox-")


async def cancel_asaas_charge(provider: str | None, provider_payment_id: str | None) -> bool:
    """DELETE /payments/{id} — cancela uma cobrança PENDENTE no Asaas (best-effort).

    Usado ao recriar cobrança (evita duplicidade) e ao expirar/cortar um passeio
    não pago. Nunca levanta: loga warning e devolve False em qualquer falha.
    """
    if not _is_asaas_provider_charge(provider, provider_payment_id):
        return False
    try:
        cfg = _get_asaas_config()
        async with httpx.AsyncClient(
            base_url=cfg["base_url"],
            headers=asaas_headers(cfg["api_key"], mode="live" if cfg["is_live"] else "sandbox"),
            timeout=15,
        ) as client:
            resp = await client.delete(f"/payments/{provider_payment_id}")
            if resp.status_code >= 400:
                logger.warning(
                    "cancel_asaas_charge: Asaas retornou status_http=%s provider_payment_id=%s",
                    resp.status_code, provider_payment_id,
                )
                return False
            logger.info("cancel_asaas_charge: cobrança cancelada provider_payment_id=%s", provider_payment_id)
            return True
    except Exception as exc:
        logger.warning(
            "cancel_asaas_charge: falha ao cancelar provider_payment_id=%s: %s",
            provider_payment_id, exc,
        )
        return False


async def refund_asaas_charge(provider: str | None, provider_payment_id: str | None) -> bool:
    """POST /payments/{id}/refund — estorna uma cobrança CONFIRMADA no Asaas.

    Usado na decisão do tutor (ação `refund`). O status final do Payment e os
    voids são tratados pelo webhook PAYMENT_REFUNDED existente. Best-effort com
    log; devolve False em falha para o caller sinalizar erro claro ao tutor.
    """
    if not _is_asaas_provider_charge(provider, provider_payment_id):
        return False
    try:
        cfg = _get_asaas_config()
        async with httpx.AsyncClient(
            base_url=cfg["base_url"],
            headers=asaas_headers(cfg["api_key"], mode="live" if cfg["is_live"] else "sandbox"),
            timeout=20,
        ) as client:
            resp = await client.post(f"/payments/{provider_payment_id}/refund", json={})
            if resp.status_code >= 400:
                logger.warning(
                    "refund_asaas_charge: Asaas retornou status_http=%s provider_payment_id=%s",
                    resp.status_code, provider_payment_id,
                )
                return False
            logger.info("refund_asaas_charge: refund solicitado provider_payment_id=%s", provider_payment_id)
            return True
    except Exception as exc:
        logger.warning(
            "refund_asaas_charge: falha ao estornar provider_payment_id=%s: %s",
            provider_payment_id, exc,
        )
        return False


def cancel_asaas_charge_sync(provider: str | None, provider_payment_id: str | None) -> bool:
    """Wrapper síncrono de cancel_asaas_charge para contextos sem await (scheduler).

    Roda a coroutine num event loop isolado em uma thread separada — seguro mesmo
    quando já existe um loop rodando na thread atual (ciclo do scheduler). Best-effort:
    nunca levanta; devolve False em qualquer erro.
    """
    if not _is_asaas_provider_charge(provider, provider_payment_id):
        return False
    import threading

    result: dict[str, bool] = {"ok": False}

    def _runner() -> None:
        try:
            result["ok"] = asyncio.run(cancel_asaas_charge(provider, provider_payment_id))
        except Exception as exc:  # pragma: no cover - defensivo
            logger.warning(
                "cancel_asaas_charge_sync: falha provider_payment_id=%s: %s",
                provider_payment_id, exc,
            )

    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    t.join(timeout=20)
    return result["ok"]


def walk_payment_cutoff_minutes() -> int:
    """Minutos antes do INÍCIO do passeio em que o pagamento ainda é aceito para
    promover ao matching (env WALK_PAYMENT_CUTOFF_MINUTES, default 45)."""
    try:
        return int(os.getenv("WALK_PAYMENT_CUTOFF_MINUTES", "45"))
    except (TypeError, ValueError):
        return 45


def _parse_walk_start_naive(scheduled_date: str | None) -> datetime | None:
    """Início do passeio (naive UTC) a partir de walk.scheduled_date (ISO). None se
    não parseável (nesse caso o corte não se aplica no webhook — libera normal)."""
    if not scheduled_date:
        return None
    raw = str(scheduled_date).strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        date_part = raw.partition("T")[0]
        try:
            return datetime.fromisoformat(date_part).replace(tzinfo=None)
        except ValueError:
            return None


def _walk_start_past_cutoff(scheduled_date: str | None) -> bool:
    """True se o início do passeio está a menos de WALK_PAYMENT_CUTOFF_MINUTES de
    agora (ou já passou). False quando a data não é parseável (não bloqueia)."""
    start = _parse_walk_start_naive(scheduled_date)
    if start is None:
        return False
    return start <= datetime.utcnow() + timedelta(minutes=walk_payment_cutoff_minutes())


PAYMENT_PENDING_STATUSES = {
    "pagamento_sandbox_criado",
    "aguardando_pagamento",
}


def _build_split_config_for_payment(db: Session, walk_id: str | None, tenant_id: str | None, split: dict) -> dict | None:
    """Monta o split_config para envio ao Asaas quando as 3 condições valem:
    1. split_enabled == True no TenantPaymentConfig
    2. Walker do walk tem asaas_wallet_id preenchido
    3. PAYMENT_MODE == asaas_live

    Retorna None se qualquer condição falhar (comportamento atual sem split).
    """
    if PAYMENT_MODE != "asaas_live":
        return None

    # Verificar split_enabled no tenant
    from app.models.tenant_payment_config import TenantPaymentConfig
    config = None
    if tenant_id:
        config = (
            db.query(TenantPaymentConfig)
            .filter(
                TenantPaymentConfig.tenant_id == tenant_id,
                TenantPaymentConfig.active.is_(True),
            )
            .first()
        )
    if not config or not config.split_enabled:
        return None

    if not walk_id:
        return None

    walk = db.get(Walk, walk_id)
    if not walk or not walk.walker_id:
        return None

    from app.models.walker_profile import WalkerProfile
    walker_profile = (
        db.query(WalkerProfile)
        .filter(WalkerProfile.user_id == walk.walker_id)
        .first()
    )
    if not walker_profile or not walker_profile.asaas_wallet_id:
        return None

    # Percentual repassado ao walker no gateway = repasse CONTÁBIL (compute_split),
    # via fonte única walker_percent_from_split (honra a margem do tenant; R2/R10).
    return {
        "wallet_id": walker_profile.asaas_wallet_id,
        "percentual_value": walker_percent_from_split(split),
    }


@router.post("/create", response_model=PaymentResponse)
async def create_payment(payload: PaymentCreate, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    from app.dependencies.legal_gate import enforce_legal_acceptance
    enforce_legal_acceptance(request, user, db)
    # Valida o modo antes de qualquer coisa (levanta 400 para modo desconhecido,
    # 503 para live sem chave configurada).
    cfg = _get_asaas_config()
    is_live = cfg["is_live"]

    # IDOR-1: valida ownership do walk ANTES de qualquer cobrança.
    # Se o walk existe no banco e pertence a outro tutor → 404 (bloqueia IDOR).
    # Se o walk nao existe (ex.: referencia de idempotencia legada) → permite seguir.
    if payload.walk_id:
        _walk_ref = db.get(Walk, payload.walk_id)
        if _walk_ref is not None and _walk_ref.tutor_id != user.id:
            raise HTTPException(status_code=404, detail="Passeio nao encontrado.")

    # Projeto A: passeio coberto por assinatura não gera cobrança avulsa.
    if payload.walk_id:
        _covered = db.get(Walk, payload.walk_id)
        if _covered is not None and getattr(_covered, "subscription_id", None):
            raise HTTPException(status_code=409, detail="Este passeio já está coberto pelo seu plano mensal.")

        # P2: quando há walk_id, o `amount` NÃO é confiável (vem do client). Casa
        # com a cotação server-authoritative (build_quote = preço do walk - desconto
        # de plano do tenant). Rejeita subcotação/supercotação para evitar que o
        # tutor pague um valor arbitrário por um passeio. Tolerância de 1 centavo
        # para ruído de float.
        if _covered is not None:
            _quote_total = round(float(build_quote(db, _covered.tenant_id, _covered.price)["total"]), 2)
            if abs(round(float(payload.amount), 2) - _quote_total) > 0.01:
                logger.warning(
                    "create_payment.amount_mismatch walk_id=%s amount=%s quote_total=%s tutor=%s",
                    payload.walk_id, payload.amount, _quote_total, user.id,
                )
                raise HTTPException(
                    status_code=400,
                    detail="Valor do pagamento não confere com a cotação do passeio.",
                )

    # Idempotencia + recriação sem duplicar (item B):
    # - Pendente RECENTE (< 2 min): duplo-clique/retry → devolve o existente (não recria).
    # - Pendente ANTIGO do mesmo walk (PIX que expirou no dia, nova tentativa do tutor):
    #   cancela a cobrança pendente no Asaas (DELETE, best-effort) e marca o Payment local
    #   como `cancelado_regenerado` (NÃO conta como pendente nem pago) ANTES de criar a nova.
    #   Sem isso, ficariam duas cobranças pendentes para o mesmo passeio.
    if payload.walk_id:
        _recent_cutoff = datetime.utcnow() - timedelta(minutes=2)
        existing = (
            db.query(Payment)
            .filter(
                Payment.walk_id == payload.walk_id,
                Payment.status.in_(PAYMENT_PENDING_STATUSES),
                Payment.created_at >= _recent_cutoff,
            )
            .first()
        )
        if existing:
            logger.warning(
                "create_payment.idempotente walk_id=%s payment_id=%s status=%s",
                payload.walk_id,
                existing.id,
                existing.status,
            )
            return payment_response(
                existing,
                method=payload.method,
                sandbox_message="Pagamento ja existente devolvido (idempotencia). Nenhuma nova cobranca foi criada.",
            )
        # Regenera: cancela pendentes antigos do walk (Asaas + local) antes de criar a nova.
        _stale_pendings = (
            db.query(Payment)
            .filter(
                Payment.walk_id == payload.walk_id,
                Payment.status.in_(PAYMENT_PENDING_STATUSES),
            )
            .all()
        )
        for _old in _stale_pendings:
            await cancel_asaas_charge(_old.provider, _old.provider_payment_id)
            _old.status = "cancelado_regenerado"
            db.add(_old)
        if _stale_pendings:
            db.commit()
            logger.info(
                "create_payment.regenerado walk_id=%s cancelados=%s",
                payload.walk_id, len(_stale_pendings),
            )
    else:
        # Pagamento avulso (walk_id=None): dedup por tutor + amount em janela de 2 min
        # para evitar cobranças duplas por duplo-clique ou retry do client.
        _dedup_cutoff = datetime.utcnow() - timedelta(minutes=2)
        existing_avulso = (
            db.query(Payment)
            .filter(
                Payment.tutor_id == user.id,
                Payment.walk_id.is_(None),
                Payment.amount == payload.amount,
                Payment.status.in_(PAYMENT_PENDING_STATUSES),
                Payment.created_at >= _dedup_cutoff,
            )
            .first()
        )
        if existing_avulso:
            logger.warning(
                "create_payment.idempotente_avulso tutor_id=%s amount=%s payment_id=%s status=%s",
                user.id,
                payload.amount,
                existing_avulso.id,
                existing_avulso.status,
            )
            return payment_response(
                existing_avulso,
                method=payload.method,
                sandbox_message="Pagamento avulso ja existente devolvido (idempotencia). Nenhuma nova cobranca foi criada.",
            )

    # Fase 1 Passo 4 §D: deriva walker_id do walk para usar comissão por par.
    # Se o walk não existir ou não tiver walker atribuído, walker_id=None →
    # comportamento idêntico ao original (zero-regressão).
    _walk_for_split = db.get(Walk, payload.walk_id) if payload.walk_id else None
    _walker_id_for_split = _walk_for_split.walker_id if _walk_for_split else None
    # A4 (Modelo B): o pagamento pertence ao TENANT ATIVO (header X-Tenant-Slug →
    # request.state.tenant_id), igual ao walk. Alinha o split/comissão e o
    # Payment.tenant_id ao GUC RLS da sessão (evita WITH CHECK violation). Sem header
    # cai em user.tenant_id → zero-regressão (app base/single-tenant).
    _payment_tenant_id = getattr(request.state, "tenant_id", None) or user.tenant_id
    split = build_payment_split(db, _payment_tenant_id, payload.amount, walker_id=_walker_id_for_split)
    split_config = _build_split_config_for_payment(db, payload.walk_id, _payment_tenant_id, split)
    # Injeta split_config via ContextVar (async-safe) antes de chamar create_asaas_payment,
    # mantendo a assinatura pública (payload, user) compatível com mocks de teste.
    _split_config_ctx.set(split_config)

    # Carrega CPF do TutorProfile e injeta via ContextVar (async-safe).
    # Em modo live sem CPF válido, create_asaas_customer levantará 400.
    from app.models.tutor_profile import TutorProfile as _TutorProfile
    _tp = db.query(_TutorProfile).filter(_TutorProfile.user_id == user.id).first()
    _tutor_cpf = (_tp.cpf or "").strip() if _tp else ""
    _tutor_cpf_ctx.set(_tutor_cpf if len(_tutor_cpf) == 11 else None)

    try:
        provider_data, pix_data, _billing_type = await create_asaas_payment(payload, user)
        provider_status = provider_data.get("status")
    except HTTPException:
        # Erros de configuração (503 live sem chave, 400 modo desconhecido) propagam direto.
        raise
    except Exception as error:
        if is_live:
            # No modo live não usamos fallback interno — falha explícita é mais segura.
            logger.error(
                "Asaas Live indisponivel. error=%s user_id=%s walk_id=%s tenant_id=%s",
                error, user.id, payload.walk_id, user.tenant_id,
            )
            raise HTTPException(
                status_code=502,
                detail="Gateway de pagamento em produção indisponível. Tente novamente em instantes.",
            )
        logger.warning(
            "Asaas Sandbox indisponivel; usando fallback interno beta. error=%s user_id=%s walk_id=%s tenant_id=%s",
            error, user.id, payload.walk_id, user.tenant_id,
        )
        provider_data = {
            "id": f"internal-sandbox-{uuid4()}",
            "status": "PAYMENT_CREATED",
            "invoiceUrl": None,
            "bankSlipUrl": None,
        }
        pix_data = {}
        provider_status = provider_data.get("status")

    provider_name = "asaas_live" if is_live else "asaas_sandbox"
    invoice_url = provider_data.get("invoiceUrl") or provider_data.get("bankSlipUrl")

    payment = Payment(
        id=str(uuid4()),
        tenant_id=_payment_tenant_id,
        tutor_id=user.id,
        walk_id=payload.walk_id,
        amount=payload.amount,
        status=normalize_payment_status(provider_status),
        provider=provider_name,
        provider_payment_id=provider_data.get("id"),
        invoice_url=invoice_url,
        commission_percent=split["commission_percent"],
        platform_amount=split["platform_amount"],
        walker_amount=split["walker_amount"],
    )
    db.add(payment)
    db.commit()
    db.refresh(payment)

    sandbox_msg = (
        None if is_live
        else "Cobranca criada no Asaas Sandbox. Nenhuma cobranca real sera realizada."
    )

    return payment_response(
        payment,
        method=payload.method,
        provider_status=provider_status,
        invoice_url=invoice_url,
        pix_qr_code=pix_data.get("encodedImage"),
        pix_copy_paste=pix_data.get("payload"),
        pix_expiration_date=pix_data.get("expirationDate"),
        sandbox_message=sandbox_msg,
    )


@router.get("/quote", response_model=PaymentQuoteResponse)
def get_payment_quote(walk_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Cotação por tenant (R4): preço do passeio, desconto de plano e total.

    Fonte de verdade do total a cobrar (o app não calcula taxa/desconto localmente).
    Sem taxa de serviço (R$5 removida). 404 quando o walk não é do solicitante
    (não revela existência de passeios de outros tutores).
    """
    walk = db.get(Walk, walk_id)
    is_admin = user.role in {"admin", "super_admin"}
    if not walk or (walk.tutor_id != user.id and not is_admin):
        raise HTTPException(status_code=404, detail="Passeio nao encontrado.")
    quote = build_quote(db, walk.tenant_id, walk.price)
    return PaymentQuoteResponse(**quote)


async def _fetch_pix_data_for_payment(payment: Payment) -> dict:
    """Re-busca pixQrCode no Asaas para cobranças PIX pendentes.

    Retorna dict com encodedImage/payload/expirationDate quando bem-sucedido,
    ou {} em qualquer falha (timeout, 4xx/5xx, provider desconhecido, etc.).
    Nunca levanta exceção — o GET /payments/{id} não deve falhar por causa disso.

    Condições para chamar o Asaas (todas devem valer):
    - provider em {asaas_live, asaas_sandbox}
    - status em PAYMENT_PENDING_STATUSES
    - provider_payment_id preenchido e sem prefixo "internal-sandbox-" (fallback
      interno que não existe no Asaas e causaria 404 desnecessário)
    """
    provider = payment.provider or ""
    if provider not in {"asaas_live", "asaas_sandbox"}:
        return {}
    if payment.status not in PAYMENT_PENDING_STATUSES:
        return {}
    pid = payment.provider_payment_id or ""
    if not pid or pid.startswith("internal-sandbox-"):
        return {}

    try:
        cfg = _get_asaas_config()
        async with httpx.AsyncClient(
            base_url=cfg["base_url"],
            headers=asaas_headers(cfg["api_key"], mode="live" if cfg["is_live"] else "sandbox"),
            timeout=10,
        ) as client:
            resp = await client.get(f"/payments/{pid}/pixQrCode")
            if resp.status_code >= 400:
                logger.warning(
                    "_fetch_pix_data_for_payment: Asaas retornou status_http=%s payment_id=%s provider_payment_id=%s",
                    resp.status_code, payment.id, pid,
                )
                return {}
            data = resp.json()
            return {
                "pix_qr_code": data.get("encodedImage"),
                "pix_copy_paste": data.get("payload"),
                "pix_expiration_date": data.get("expirationDate"),
            }
    except Exception as exc:
        logger.warning(
            "_fetch_pix_data_for_payment: falha ao buscar pixQrCode payment_id=%s: %s",
            payment.id, exc,
        )
        return {}


@router.get("/{payment_id}", response_model=PaymentResponse)
async def get_payment(payment_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    payment = db.get(Payment, payment_id)
    # Retorna 404 (e nao 403) quando o pagamento nao e do solicitante para nao
    # revelar a existencia de pagamentos de outros usuarios via enumeracao de ID.
    is_admin = user.role in {"admin", "super_admin"}
    if not payment or (payment.tutor_id != user.id and not is_admin):
        raise HTTPException(status_code=404, detail="Pagamento nao encontrado.")
    pix_extra = await _fetch_pix_data_for_payment(payment)
    return payment_response(payment, **pix_extra)


_PAYMENT_CONFIRMED_STATUS = "pagamento_confirmado_sandbox"
_PAYMENT_CONFIRMED_EVENTS = {"PAYMENT_CONFIRMED", "PAYMENT_RECEIVED", "PAYMENT_DUNNING_RECEIVED"}

# R3 — estado de estorno consumado, DISTINTO de 'falha_pagamento' (que é falha de
# cobrança). Auditável: um pagamento confirmado que foi estornado não deve ser
# confundido com uma cobrança que nunca liquidou.
_PAYMENT_REFUNDED_STATUS = "pagamento_estornado"
# Eventos que consumam o estorno e tiram o pagamento do estado confirmado.
# PAYMENT_REVERSED: estorno bancário direto (ex.: Pix devolvido), tratado igual
# a PAYMENT_REFUNDED — leva o Payment a pagamento_estornado, consistente com
# o void do ganho já presente em _WALKER_EARNING_VOID_EVENTS.
_PAYMENT_REFUND_EVENTS = {"PAYMENT_REFUNDED", "PAYMENT_REVERSED"}
# Estados terminais "pegajosos": uma vez aqui, eventos de cobrança comuns NÃO
# regridem o status (só um estorno consumado pode sair de confirmado).
_PAYMENT_STICKY_STATUSES = {_PAYMENT_CONFIRMED_STATUS, _PAYMENT_REFUNDED_STATUS}

# Fase 3: eventos que ANULAM (void) o ganho do passeador do walk associado ao Payment.
# Cobre apenas pagamentos de passeio AVULSO (Payment.walk_id preenchido).
# Passeio de REDE pago por crédito tem o refund no Payment da COMPRA do crédito
# (sem walk_id do passeio) — esse caso é coberto pelo void MANUAL via endpoint admin.
_WALKER_EARNING_VOID_EVENTS = {
    "PAYMENT_REFUNDED",
    "PAYMENT_CHARGEBACK_REQUESTED",
    "PAYMENT_CHARGEBACK_DISPUTE",
    "PAYMENT_REVERSED",
}


def resolve_payment_webhook_status(current_status: str | None, event: str | None, fallback_status: str | None) -> str | None:
    """Decide o novo status de um Payment a partir de um evento de webhook do
    Asaas, com idempotência e anti-retrocesso (R3).

    Regras (nesta ordem):
    1. Estorno consumado (PAYMENT_REFUNDED) leva ao estado de estorno DISTINTO,
       mesmo a partir de confirmado — é uma transição legítima e auditável.
    2. Status terminal (confirmado/estornado) é pegajoso: eventos de cobrança
       comuns não o regridem (ex.: PAYMENT_OVERDUE atrasado após PAYMENT_CONFIRMED
       é ignorado; reentrega de PAYMENT_CONFIRMED é no-op).
    3. Caso contrário, aplica o mapeamento do evento (ou o fallback do provider).
    """
    if event in _PAYMENT_REFUND_EVENTS:
        return _PAYMENT_REFUNDED_STATUS
    if current_status in _PAYMENT_STICKY_STATUSES:
        return current_status
    return STATUS_BY_WEBHOOK_EVENT.get(event, fallback_status)


def _create_payment_confirmed_notification(db: Session, payment: Payment) -> None:
    """Cria notificação de pagamento confirmado para o tutor — idempotente.

    Não cria duplicata se já existe notificação do tipo payment_confirmed para este walk/payment.
    """
    if not payment.walk_id:
        return

    walk = db.get(Walk, payment.walk_id)
    if not walk:
        return

    # Idempotência: checar se já existe notificação deste tipo para este payment
    existing = (
        db.query(Notification)
        .filter(
            Notification.user_id == walk.tutor_id,
            Notification.type == "payment_confirmed",
            Notification.related_entity_id == payment.id,
            Notification.related_entity_type == "payment",
        )
        .first()
    )
    if existing:
        logger.info("notificação payment_confirmed já existe para payment_id=%s, pulando", payment.id)
        return

    # Importa _create_notification localmente para evitar ciclo de imports
    from app.routes.notifications import NotificationCreate, _create_notification

    notif_payload = NotificationCreate(
        user_id=walk.tutor_id,
        user_role="tutor",
        title="Pagamento confirmado!",
        message="Seu passeio está garantido. 🐾",
        type="payment_confirmed",
        related_entity_type="payment",
        related_entity_id=payment.id,
        metadata={"walk_id": payment.walk_id, "amount": payment.amount},
    )
    _create_notification(db, notif_payload)
    logger.info("notificação payment_confirmed criada para tutor_id=%s payment_id=%s", walk.tutor_id, payment.id)


def _handle_tip_webhook(db, event: str, payment_data: dict) -> bool:
    """Processa webhooks cujo externalReference começa com 'tip:'.

    Retorna True se tratou um tip, False caso contrário.
    Idempotente: não duplica notificação ao walker.
    """
    from app.models.walk_tip import WalkTip

    external_ref = payment_data.get("externalReference") or ""
    provider_payment_id = payment_data.get("id")

    tip: WalkTip | None = None

    # Resolução primária: por externalReference
    if external_ref.startswith("tip:"):
        tip_id = external_ref[4:]
        tip = db.get(WalkTip, tip_id)

    # Fallback: por provider_payment_id
    if tip is None and provider_payment_id:
        tip = (
            db.query(WalkTip)
            .filter(WalkTip.provider_payment_id == provider_payment_id)
            .first()
        )

    if tip is None:
        return False

    # Atualiza status da gorjeta
    new_status = STATUS_BY_WEBHOOK_EVENT.get(event, normalize_payment_status(payment_data.get("status")))
    if new_status in (_PAYMENT_CONFIRMED_STATUS,):
        tip.status = "paid"
        tip.paid_at = datetime.utcnow()
    elif new_status == "falha_pagamento":
        tip.status = "failed"
    else:
        # pending/waiting — mantém como pending
        tip.status = "pending"

    if provider_payment_id and not tip.provider_payment_id:
        tip.provider_payment_id = provider_payment_id

    db.add(tip)

    # Notificação crítica para o walker ao confirmar gorjeta (idempotente)
    if tip.status == "paid":
        from app.models.notification import Notification
        from app.routes.notifications import NotificationCreate, _create_notification

        existing = (
            db.query(Notification)
            .filter(
                Notification.user_id == tip.walker_id,
                Notification.type == "tip_received",
                Notification.related_entity_id == tip.id,
            )
            .first()
        )
        if not existing:
            try:
                notif_payload = NotificationCreate(
                    user_id=tip.walker_id,
                    user_role="walker",
                    title="Você recebeu uma gorjeta! 🎉",
                    message=f"O tutor enviou R$ {tip.amount:.2f} como agradecimento pelo passeio.",
                    type="tip_received",
                    related_entity_type="walk_tip",
                    related_entity_id=tip.id,
                    metadata={"walk_id": tip.walk_id, "amount": tip.amount},
                )
                _create_notification(db, notif_payload)
                logger.info(
                    "notificação tip_received criada para walker_id=%s tip_id=%s",
                    tip.walker_id, tip.id,
                )
            except Exception:
                logger.exception("falha ao criar notificação tip_received tip_id=%s", tip.id)

    try:
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("_handle_tip_webhook: falha ao persistir tip_id=%s", tip.id if tip else "desconhecido")
        raise

    # Provisão fiscal best-effort (gorjeta confirmada)
    if tip.status == "paid":
        import types as _types
        _tip_payment_like = _types.SimpleNamespace(
            id=provider_payment_id or f"tip:{tip.id}",
            amount=float(tip.amount),
            platform_amount=None,
            walker_amount=None,
        )
        _provision_safe(db, tip.tenant_id, _tip_payment_like, REVENUE_TIP)

    return True


def _handle_tenant_saas_subscription_webhook(db, event: str, payment_data: dict) -> bool:
    """Processa webhooks de mensalidade SaaS do tenant (Projeto B).

    Identifica eventos cujo externalReference começa com 'tenant_sub:'.
    Atualiza TenantSaasSubscription e, em confirmação, reativa o Tenant se
    suspenso por billing. Registra Payment idempotente para auditoria.
    Retorna True se tratou (ou consumiu como noop), False se não é tenant_sub.
    """
    ext = payment_data.get("externalReference") or ""
    if not ext.startswith("tenant_sub:"):
        return False

    sub_id = ext[len("tenant_sub:"):]
    sub: TenantSaasSubscription | None = db.get(TenantSaasSubscription, sub_id)
    if sub is None:
        # Fallback por asaas_subscription_id
        sub = (
            db.query(TenantSaasSubscription)
            .filter(TenantSaasSubscription.asaas_subscription_id == payment_data.get("subscription"))
            .first()
        )
    if sub is None:
        logger.warning(
            "_handle_tenant_saas_subscription_webhook: assinatura não encontrada ext=%s", ext
        )
        return True  # consumiu o evento — noop seguro

    now = datetime.utcnow()

    # --- OVERDUE: marca inadimplência ---
    if event == "PAYMENT_OVERDUE":
        sub.status = SAAS_OVERDUE
        if sub.overdue_since is None:
            sub.overdue_since = now
        db.add(sub)
        db.commit()
        return True

    # --- Confirmado: reativa assinatura + tenant (se suspenso por billing) ---
    new_status = STATUS_BY_WEBHOOK_EVENT.get(event) or STATUS_BY_ASAAS_STATUS.get(
        payment_data.get("status")
    )
    if new_status == _PAYMENT_CONFIRMED_STATUS:
        sub.status = SAAS_ACTIVE
        sub.last_payment_at = now
        sub.overdue_since = None
        sub.current_period_start = now
        # P1: usar o mesmo cálculo de fim-de-mês da CRIAÇÃO (overflow-safe), não
        # timedelta(days=31). O +31 dias desalinha o vencimento (ex.: confirmar em
        # 31/jan cairia em 03/mar, "pulando" fevereiro) e desloca o ciclo a cada mês.
        from app.services.tenant_saas_billing_service import _period_end_month
        sub.current_period_end = _period_end_month(now)
        db.add(sub)

        # Reativa tenant apenas se suspenso por inadimplência (não por suspensão manual)
        tenant: Tenant | None = db.get(Tenant, sub.tenant_id)
        if tenant and tenant.status == "suspended" and tenant.suspended_reason == "billing":
            tenant.status = "active"
            tenant.suspended_reason = None
            db.add(tenant)

        # Payment idempotente para auditoria financeira
        pid = payment_data.get("id")
        if pid and not db.query(Payment).filter(Payment.provider_payment_id == pid).first():
            db.add(Payment(
                id=str(uuid4()),
                tenant_id=sub.tenant_id,
                tutor_id=sub.tenant_id,  # sentinela: SaaS não tem tutor físico
                walk_id=None,
                amount=float(sub.price),
                status=new_status,
                provider="asaas_tenant_saas",
                provider_payment_id=pid,
            ))

        db.commit()

        # ---- NFS-e best-effort (Projeto NFS-e, dormente por NFS_E_ENABLED=false) --------
        # Emite nota fiscal para mensalidade SaaS confirmada.
        # NUNCA propaga exceção: falha de NFS-e jamais pode quebrar o webhook de cobrança.
        # Chamada async dentro de handler sync (FastAPI roda sync routes em thread pool,
        # portanto não há event loop ativo nesta thread — asyncio.run() é seguro aqui).
        #
        # TODO: comissão de passeio — NÃO implementada aqui. A base de cálculo (valor
        # líquido ao passeador vs. valor bruto do pagamento) depende de definição do
        # contador. Quando implementada, seguirá o mesmo padrão best-effort abaixo.
        try:
            from app.services.nfse_service import issue_nfse_for_saas_payment
            asyncio.run(
                issue_nfse_for_saas_payment(
                    db,
                    tenant_id=sub.tenant_id,
                    asaas_payment_id=pid or f"saas:{sub.id}:{now.date().isoformat()}",
                    value=float(sub.price),
                    subscription_id=sub.asaas_subscription_id,
                )
            )
        except Exception:
            logger.exception(
                "_handle_tenant_saas_subscription_webhook: falha best-effort NFS-e tenant_id=%s",
                sub.tenant_id,
            )
            # rollback parcial apenas da NFS-e; o pagamento já foi commitado acima.
            try:
                db.rollback()
            except Exception:
                pass

        # Provisão fiscal best-effort (mensalidade SaaS confirmada)
        import types as _types
        _saas_payment_like = _types.SimpleNamespace(
            id=pid or f"saas:{sub.id}",
            amount=float(sub.price),
            platform_amount=None,
            walker_amount=None,
        )
        _provision_safe(db, sub.tenant_id, _saas_payment_like, REVENUE_SAAS_SUBSCRIPTION)

        return True

    # Evento não tratado (ex.: PAYMENT_CREATED, PAYMENT_UPDATED) — noop seguro
    return True


def _handle_subscription_webhook(db, event: str, payment_data: dict) -> bool:
    """Processa webhooks de cobranças geradas por subscriptions Asaas.

    Cria/atualiza Payment local vinculado ao tutor para aparecer no financeiro
    e notifica o tutor na confirmação.
    Retorna True se tratou, False caso contrário.
    """
    external_ref = payment_data.get("externalReference") or ""
    # Guard negativo: tenant_sub: é tratado ANTES deste handler no dispatcher.
    if (payment_data.get("externalReference") or "").startswith("tenant_sub:"):
        return False
    if not external_ref.startswith("sub:"):
        return False

    # Cobranças geradas pela subscription têm subscription_id no payload
    subscription_id_asaas = payment_data.get("subscription")
    if not subscription_id_asaas:
        # Evento de subscription sem campo subscription — não é uma cobrança de assinatura
        return False

    from app.models.recurring_plan import TutorSubscription
    from app.routes.notifications import NotificationCreate, _create_notification

    sub_local_id = external_ref[4:]
    sub = db.get(TutorSubscription, sub_local_id)
    if sub is None:
        # Tenta buscar por asaas_subscription_id
        sub = (
            db.query(TutorSubscription)
            .filter(TutorSubscription.asaas_subscription_id == subscription_id_asaas)
            .first()
        )
    if sub is None:
        logger.warning("webhook sub: assinatura local não encontrada external_ref=%s", external_ref)
        return True  # consumiu o evento, nada a fazer

    provider_payment_id = payment_data.get("id")
    amount = float(payment_data.get("value") or 0)
    new_status = STATUS_BY_WEBHOOK_EVENT.get(event, normalize_payment_status(payment_data.get("status")))

    # Idempotência: reutiliza Payment existente se provider_payment_id já existe
    existing_payment = None
    if provider_payment_id:
        existing_payment = (
            db.query(Payment)
            .filter(Payment.provider_payment_id == provider_payment_id)
            .first()
        )

    if existing_payment:
        existing_payment.status = new_status
        db.add(existing_payment)
    else:
        local_payment = Payment(
            id=str(uuid4()),
            tenant_id=sub.tenant_id,
            tutor_id=sub.tutor_id,
            walk_id=None,
            amount=amount,
            status=new_status,
            provider="asaas_subscription",
            provider_payment_id=provider_payment_id,
            invoice_url=payment_data.get("invoiceUrl"),
        )
        db.add(local_payment)
        existing_payment = local_payment

    # Notificação ao tutor na confirmação (idempotente)
    if new_status == _PAYMENT_CONFIRMED_STATUS:
        from app.models.notification import Notification
        notif_check = (
            db.query(Notification)
            .filter(
                Notification.user_id == sub.tutor_id,
                Notification.type == "payment_confirmed",
                Notification.related_entity_id == existing_payment.id,
            )
            .first()
        )
        if not notif_check:
            try:
                notif_payload = NotificationCreate(
                    user_id=sub.tutor_id,
                    user_role="tutor",
                    title="Pagamento da assinatura confirmado!",
                    message=f"Sua mensalidade de R$ {amount:.2f} foi confirmada.",
                    type="payment_confirmed",
                    related_entity_type="payment",
                    related_entity_id=existing_payment.id,
                    metadata={"subscription_id": sub.id, "amount": amount},
                )
                _create_notification(db, notif_payload)
            except Exception:
                logger.exception("falha ao notificar tutor subscription payment tutor_id=%s", sub.tutor_id)

        # REDE DE PROTEÇÃO (item 6): uma assinatura recorrente confirmada NÃO deveria
        # existir quando o tenant está no plano free (`recurring_plans` é bloqueada por
        # plano). Isso só acontece se alguma subscription escapou do cancelamento no
        # downgrade do reverse trial (maybe_downgrade_expired_trial). Detecta, LOGA
        # estruturado e dispara alerta operacional — mas AINDA concede os créditos
        # (contrapartida do pagamento já cobrado: "nenhuma cobrança sem contrapartida").
        try:
            from app.models.tenant import Tenant as _Tenant
            from app.services.tenant_free_plan_service import plan_blocks_feature
            _tenant = db.get(_Tenant, sub.tenant_id)
            if _tenant is not None and plan_blocks_feature(_tenant, "recurring_plans"):
                logger.error(
                    "subscription_payment_on_blocked_plan: assinatura recorrente cobrada "
                    "em tenant sem recurring_plans (subscription escapou do cancelamento "
                    "do downgrade). tenant_id=%s subscription_id=%s tutor_id=%s "
                    "asaas_subscription_id=%s provider_payment_id=%s amount=%.2f",
                    sub.tenant_id, sub.id, sub.tutor_id,
                    getattr(sub, "asaas_subscription_id", None), provider_payment_id, amount,
                )
                try:
                    from app.services.admin_operational_event_service import (
                        record_admin_operational_event,
                    )
                    record_admin_operational_event(
                        db,
                        event_type="subscription_payment_on_blocked_plan",
                        entity_type="tutor_subscription",
                        entity_id=sub.id,
                        title="Cobrança de assinatura em plano sem recorrência",
                        description=(
                            "Uma assinatura recorrente confirmou pagamento em um tenant "
                            "cujo plano bloqueia recurring_plans (provável assinatura zumbi "
                            "que escapou do cancelamento no fim do reverse trial). "
                            "Cancele a assinatura no gateway."
                        ),
                        severity="critical",
                        source="webhook:asaas",
                        metadata={
                            "tenant_id": sub.tenant_id,
                            "subscription_id": sub.id,
                            "tutor_id": sub.tutor_id,
                            "asaas_subscription_id": getattr(sub, "asaas_subscription_id", None),
                            "provider_payment_id": provider_payment_id,
                            "amount": amount,
                        },
                    )
                except Exception:
                    logger.exception(
                        "falha ao registrar alerta operacional de cobranca em plano bloqueado "
                        "subscription_id=%s", sub.id,
                    )
        except Exception:
            logger.exception(
                "falha na rede de protecao de cobranca em plano bloqueado subscription_id=%s",
                sub.id,
            )

        # Projeto A: 1º pagamento concede créditos; renovações rebastecem.
        from app.services.recurring_plan_service import grant_credits_on_payment, reset_credits_if_renewal
        from app.models.recurring_plan import SUBSCRIPTION_ACTIVE, SUBSCRIPTION_OVERDUE
        # Regularização: se a assinatura estava OVERDUE (inadimplente), o pagamento
        # confirmado a reativa. Não mexe em assinaturas canceladas.
        if sub.status == SUBSCRIPTION_OVERDUE:
            sub.status = SUBSCRIPTION_ACTIVE
            db.add(sub)
        grant_credits_on_payment(db, sub)
        reset_credits_if_renewal(db, sub)

    # P2 — void-de-rede AUTOMÁTICO no estorno da COMPRA DO CRÉDITO.
    # O Payment da compra de crédito NÃO tem walk_id (o void de passeio AVULSO no
    # ramo regular só cobre Payment.walk_id). Aqui o refund recai sobre a assinatura
    # (externalReference `sub:`): revertemos o que é SEGURO (créditos não usados +
    # reversão do passivo no ledger) e ALERTAMOS os admins quando há passeios de
    # rede já consumidos (não fazemos clawback automático do ganho do passeador).
    elif event in _PAYMENT_REFUND_EVENTS:
        from app.services.credit_refund_service import reverse_credit_purchase
        reverse_credit_purchase(
            db, sub, reason=f"asaas:{event}", payment_id=provider_payment_id,
        )

    # Inadimplência (P1): PAYMENT_OVERDUE marca a assinatura do tutor como OVERDUE,
    # o que bloqueia consume_credit_if_available (exige status ACTIVE). Sem isso o
    # tutor inadimplente continuava gastando créditos. Só rebaixa quem está ACTIVE
    # (não ressuscita cancelada).
    elif event == "PAYMENT_OVERDUE":
        from app.models.recurring_plan import SUBSCRIPTION_ACTIVE, SUBSCRIPTION_OVERDUE
        if sub.status == SUBSCRIPTION_ACTIVE:
            sub.status = SUBSCRIPTION_OVERDUE
            db.add(sub)
            logger.info(
                "webhook sub: assinatura %s marcada OVERDUE (tutor %s inadimplente)",
                sub.id, sub.tutor_id,
            )

    try:
        db.commit()
    except Exception:
        db.rollback()
        logger.exception(
            "_handle_subscription_webhook: falha ao persistir subscription_id=%s provider_payment_id=%s",
            sub.id if sub else "desconhecido", payment_data.get("id"),
        )
        raise
    return True


@router.post("/webhooks/asaas")
def asaas_webhook(request: Request, payload: dict, db: Session = Depends(get_global_db)):
    # ---------------------------------------------------------------------------
    # 1. Verificação de assinatura — antes de qualquer acesso ao banco.
    # ---------------------------------------------------------------------------
    expected = os.getenv("ASAAS_WEBHOOK_TOKEN")
    received = request.headers.get("asaas-access-token")

    if not expected or not secrets.compare_digest(expected, received or ""):
        raise HTTPException(status_code=401, detail="Webhook não autorizado")

    # Sec-fix: defensive guard — reject structurally malformed payloads before
    # any processing. Keeps all existing .get() accesses intact below.
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Payload de webhook inválido.")
    _event_raw = payload.get("event")
    if not isinstance(_event_raw, str) or not _event_raw.strip():
        raise HTTPException(status_code=400, detail="Campo 'event' ausente ou inválido no webhook.")
    _payment_raw = payload.get("payment")
    if _payment_raw is not None and not isinstance(_payment_raw, dict):
        raise HTTPException(status_code=400, detail="Campo 'payment' deve ser um objeto no webhook.")
    _invoice_raw = payload.get("invoice")
    if _invoice_raw is not None and not isinstance(_invoice_raw, dict):
        raise HTTPException(status_code=400, detail="Campo 'invoice' deve ser um objeto no webhook.")

    event = payload.get("event")
    payment_data = payload.get("payment") or {}
    provider_payment_id = payment_data.get("id")
    external_ref = payment_data.get("externalReference") or ""

    # ---------------------------------------------------------------------------
    # Dedup persistente por event-id (P1). O Asaas envia um `id` de EVENTO no topo
    # do payload; um reenvio (retry do provedor, janela de falha parcial) traria o
    # MESMO event-id. Gravamos o event-id com UNIQUE e commitamos ANTES dos
    # handlers: se já existir, o evento JÁ foi processado → 200 sem reaplicar efeito.
    #
    # Ordenação: gravamos o marcador ANTES do efeito. Se um handler downstream
    # falhar, ele levanta 500 (Asaas reenvia); para não engolir o reenvio, o marcador
    # é REMOVIDO no caminho de erro (ver os `except` dos handlers, que chamam
    # _drop_webhook_marker). Assim: sucesso → marcador persiste (bloqueia duplicata);
    # falha → marcador some (o reenvio reprocessa).
    #
    # Sem id no topo (ex.: alguns eventos de sandbox): seguimos sem dedup
    # (best-effort) — a idempotência por provider_payment_id ainda cobre.
    _webhook_event_id = payload.get("id")
    _dedup_marked = False
    if _webhook_event_id:
        from sqlalchemy.exc import IntegrityError
        from app.models.webhook_event import WebhookEvent
        db.add(WebhookEvent(
            event_id=str(_webhook_event_id),
            provider="asaas",
            event_type=event if isinstance(event, str) else None,
        ))
        try:
            db.commit()
            _dedup_marked = True
        except IntegrityError:
            db.rollback()
            logger.info(
                "asaas_webhook.duplicate_event event_id=%s event=%s — ignorado (ja processado)",
                _webhook_event_id, event,
            )
            return {"ok": True, "received": event, "duplicate": True}

    def _drop_webhook_marker() -> None:
        """Remove o marcador de dedup quando o processamento falha (para o reenvio
        do Asaas poder reprocessar). Best-effort — nunca mascara o erro original."""
        if not _dedup_marked:
            return
        try:
            from app.models.webhook_event import WebhookEvent as _WE
            db.query(_WE).filter(_WE.event_id == str(_webhook_event_id)).delete()
            db.commit()
        except Exception:
            logger.exception(
                "asaas_webhook: falha ao remover marcador de dedup event_id=%s",
                _webhook_event_id,
            )

    # ---------------------------------------------------------------------------
    # INVOICE_* — eventos de NFS-e (nota fiscal). Processados ANTES dos eventos
    # PAYMENT_* para evitar qualquer interferência com o caminho de dinheiro.
    # Dormente enquanto a tabela nfse estiver vazia (flag NFS_E_ENABLED=false).
    # ---------------------------------------------------------------------------
    if isinstance(event, str) and event.startswith("INVOICE"):
        try:
            _handle_nfse_webhook(db, event, payload.get("invoice") or {})
        except Exception:
            db.rollback()
            logger.exception(
                "asaas_webhook.nfse_error event=%s",
                event,
            )
            _drop_webhook_marker()
            raise HTTPException(status_code=500, detail="Erro ao processar webhook de NFS-e.")
        return {"ok": True, "received": event}

    # ---------------------------------------------------------------------------
    # TRANSFER_* — eventos de transferência PIX ao passeador (Fase 3).
    # Devem ser tratados ANTES da lógica de PAYMENT_* pois o payload usa o campo
    # "transfer" (não "payment") — cair no ramo regular geraria lookup por id None.
    # TRANSFER_FAILED reverte o saque (Payment provider='pix') para 'pending' para
    # que o admin possa tentar novamente. Demais eventos TRANSFER_* são no-op (200).
    # ---------------------------------------------------------------------------
    if isinstance(event, str) and event.startswith("TRANSFER_"):
        if event == "TRANSFER_FAILED":
            transfer = payload.get("transfer") or {}
            tr_id = transfer.get("id")
            if tr_id:
                wd = db.query(Payment).filter(
                    Payment.provider_payment_id == tr_id,
                    Payment.provider == "pix",
                ).first()
                if wd:
                    wd.status = "pending"  # reverte p/ o admin tentar de novo
                    db.commit()
                    logger.info(
                        "asaas_webhook.transfer_failed tr_id=%s payment_id=%s revertido para pending",
                        tr_id, wd.id,
                    )
                else:
                    logger.warning(
                        "asaas_webhook.transfer_failed tr_id=%s sem Payment pix local",
                        tr_id,
                    )
        return {"ok": True, "received": event}

    # ---------------------------------------------------------------------------
    # 2. Processamento de DB com escopo global — via get_global_db (rls_tenant="*").
    #
    # Webhooks confiáveis processam pagamentos de QUALQUER tenant → escopo global
    # após validar a assinatura. O futuro webhook do Efí DEVE usar este mesmo helper.
    #
    # Não usamos Depends(get_db) aqui: get_db escopa a sessão ao tenant resolvido
    # da requisição HTTP (tenant padrão); com RLS ativo, isso silenciosamente ignora
    # pagamentos de outros tenants. get_global_db sempre usa rls_tenant="*" e pode
    # ser substituído via dependency_overrides nos testes.
    # ---------------------------------------------------------------------------

    # NOTA SOBRE IDEMPOTÊNCIA E STATUS CODE:
    # A dedup persistente por event-id agora EXISTE (tabela webhook_events, gravada
    # no topo deste handler): um reenvio do mesmo event-id retorna 200 sem reaplicar
    # efeito. Além disso, _handle_tip_webhook / _handle_subscription_webhook já são
    # idempotentes por design (upsert baseado em provider_payment_id / localização
    # de registro existente) — defesa em profundidade.
    # Decisão de status code:
    #   - Em caso de erro de persistência retornamos 500 (não 200) para que o
    #     Asaas reenvia o evento. O risco de duplicata em reenvio é mitigado pela
    #     idempotência das queries de lookup; sem dedup persistente é o melhor
    #     equilíbrio possível SEM migration.
    #   - 200 só é retornado quando o evento foi processado com sucesso OU quando
    #     é um evento "noop" legítimo (pagamento órfão, assinatura não encontrada).

    # --- Gorjeta ---
    if external_ref.startswith("tip:") or (
        provider_payment_id
        and not external_ref.startswith("sub:")
        and not external_ref.startswith("tenant_sub:")
        and _is_tip_payment(db, provider_payment_id, external_ref)
    ):
        try:
            _handle_tip_webhook(db, event, payment_data)
        except Exception:
            db.rollback()
            logger.exception(
                "asaas_webhook.tip_error event=%s provider_payment_id=%s",
                event, provider_payment_id,
            )
            _drop_webhook_marker()
            raise HTTPException(status_code=500, detail="Erro interno ao processar webhook de gorjeta.")
        return {"ok": True, "received": event}

    # --- Mensalidade SaaS do tenant (Projeto B) ---
    if external_ref.startswith("tenant_sub:"):
        try:
            handled = _handle_tenant_saas_subscription_webhook(db, event, payment_data)
        except Exception:
            db.rollback()
            logger.exception(
                "asaas_webhook.tenant_saas_error event=%s provider_payment_id=%s",
                event, provider_payment_id,
            )
            _drop_webhook_marker()
            raise HTTPException(status_code=500, detail="Erro ao processar webhook de mensalidade.")
        if handled:
            return {"ok": True, "received": event}

    # --- Comissão medida do tenant (Fase 1) ---
    if external_ref.startswith("tenant_comm:") and event in _PAYMENT_CONFIRMED_EVENTS:
        try:
            from app.services.commission_billing_service import mark_commission_paid
            mark_commission_paid(db, provider_payment_id)
            db.commit()
        except Exception:
            db.rollback()
            logger.exception(
                "asaas_webhook.tenant_comm_error event=%s provider_payment_id=%s",
                event, provider_payment_id,
            )
            _drop_webhook_marker()
            raise HTTPException(status_code=500, detail="Erro ao processar webhook de comissão.")
        return {"ok": True, "received": event}

    # --- Cobrança de assinatura recorrente ---
    if external_ref.startswith("sub:") or payment_data.get("subscription"):
        try:
            handled = _handle_subscription_webhook(db, event, payment_data)
        except Exception:
            db.rollback()
            logger.exception(
                "asaas_webhook.subscription_error event=%s provider_payment_id=%s",
                event, provider_payment_id,
            )
            _drop_webhook_marker()
            raise HTTPException(status_code=500, detail="Erro interno ao processar webhook de assinatura.")
        if handled:
            return {"ok": True, "received": event}

    # --- Pagamento regular de passeio ---
    try:
        payment = None
        if provider_payment_id:
            payment = db.query(Payment).filter(Payment.provider_payment_id == provider_payment_id).first()
            if payment:
                # R3: máquina de estados idempotente + anti-retrocesso. Não sobrescreve
                # um pagamento confirmado com OVERDUE/REFUNDED atrasado; estorno vai
                # para estado distinto.
                fallback = normalize_payment_status(payment_data.get("status"))
                new_status = resolve_payment_webhook_status(payment.status, event, fallback)
                status_changed = new_status != payment.status
                if status_changed:
                    payment.status = new_status
                    db.add(payment)

                # F1.3: notificação de pagamento confirmado (idempotente)
                if event in _PAYMENT_CONFIRMED_EVENTS or new_status == _PAYMENT_CONFIRMED_STATUS:
                    try:
                        _create_payment_confirmed_notification(db, payment)
                    except Exception:
                        logger.exception("falha ao criar notificação de pagamento confirmado payment_id=%s", payment.id)

                # R7: pagamento liquidado libera o walk do estado de espera ('awaiting_payment')
                # para o fluxo operacional/matching. Só age sobre walks que estavam à espera
                # (criados com o gate REQUIRE_PAYMENT_BEFORE_MATCHING ligado) — no-op caso contrário.
                #
                # Defesa contra RACE (item D): se o pagamento confirma DEPOIS do corte
                # (walk já cancelado por expiração, OU início−45min já estourou), NÃO
                # promove para matching. O dinheiro está rastreado (payment fica
                # confirmado); o walk vai para 'awaiting_tutor_reconfirmation' com motivo
                # 'pagamento_apos_corte' e o tutor decide (reagendar / trocar passeador /
                # estorno) via POST /walks/{id}/tutor-decision.
                if new_status == _PAYMENT_CONFIRMED_STATUS and payment.walk_id:
                    walk = db.get(Walk, payment.walk_id)
                    if walk:
                        op_status = getattr(walk, "operational_status", None)
                        past_cutoff = _walk_start_past_cutoff(walk.scheduled_date)
                        if op_status == "ride_cancelled" or (op_status == "awaiting_payment" and past_cutoff):
                            walk.operational_status = "awaiting_tutor_reconfirmation"
                            walk.status = "Aguardando confirmação do tutor"
                            walk.no_walker_reason = "pagamento_apos_corte"
                            walk.confirmation_expires_at = None
                            walk.matching_finished_at = datetime.utcnow()
                            db.add(walk)
                            logger.info(
                                "asaas_webhook.walk_pagamento_apos_corte walk_id=%s payment_id=%s prev_status=%s",
                                walk.id, payment.id, op_status,
                            )
                            try:
                                from app.services.operational_matching_service import notify_tutor_walk_event
                                notify_tutor_walk_event(
                                    db,
                                    walk,
                                    title="Pagamento recebido — decida o próximo passo",
                                    message="Pagamento recebido, mas o horário passou. Você pode reagendar, trocar o passeador ou pedir estorno.",
                                    notification_type="walk_recovery",
                                    priority="high",
                                    action="awaiting_tutor_reconfirmation",
                                    metadata={"decision_reason": "pagamento_apos_corte"},
                                )
                            except Exception:
                                logger.exception("falha ao notificar tutor de pagamento_apos_corte walk_id=%s", walk.id)
                        elif op_status == "awaiting_payment":
                            walk.operational_status = "pending_walker_confirmation"
                            walk.status = "Agendado"
                            db.add(walk)
                            logger.info("asaas_webhook.walk_liberado walk_id=%s payment_id=%s", walk.id, payment.id)

                # Fase 3: estorno/chargeback de um pagamento com walk_id anula o ganho do passeador.
                # Cobre apenas passeio AVULSO (Payment.walk_id preenchido).
                # Passeio de REDE pago por crédito: o refund recai sobre o Payment da COMPRA do
                # crédito (sem walk_id), portanto não é capturado aqui — usar void manual (Task 1).
                if event in _WALKER_EARNING_VOID_EVENTS and payment.walk_id:
                    from app.services.walker_payout_service import void_walker_earning
                    void_walker_earning(db, payment.walk_id, reason=f"asaas:{event}", source="webhook")
                    # FIX 13: estorno também reverte a COMISSÃO do tenant (COMM_VOID).
                    # Se a entrada já foi faturada, gera ajuste de crédito no mês seguinte.
                    from app.services.commission_billing_service import reverse_commission_for_walk
                    reverse_commission_for_walk(db, payment.walk_id, reason=f"asaas:{event}")

                db.commit()

                # Provisão fiscal best-effort (passeio confirmado)
                if new_status == _PAYMENT_CONFIRMED_STATUS:
                    _provision_safe(db, payment.tenant_id, payment, REVENUE_WALK_COMMISSION)
            else:
                # R3: pagamento órfão (provider_payment_id sem Payment local) NÃO é
                # silencioso — loga para auditoria e ainda responde 200 ao Asaas.
                logger.warning(
                    "asaas_webhook.orfao event=%s provider_payment_id=%s sem Payment local",
                    event,
                    provider_payment_id,
                )
    except HTTPException:
        raise
    except Exception:
        db.rollback()
        logger.exception(
            "asaas_webhook.regular_error event=%s provider_payment_id=%s",
            event, provider_payment_id,
        )
        _drop_webhook_marker()
        raise HTTPException(status_code=500, detail="Erro interno ao processar webhook de pagamento.")
    return {"ok": True, "received": event}


# ─────────────────────────── internal sweep endpoint (Task 8) ─────────────────
# Rota SEM dependency de auth no nível do router — autenticada apenas pelo
# token interno (X-Internal-Token). Chamada pelo Cloud Scheduler sem JWT.

@router.post("/internal/saas-billing/sweep")
def saas_billing_sweep(request: Request, db: Session = Depends(get_global_db)):
    import os, secrets
    from app.services.tenant_saas_billing_service import sweep_overdue_tenants
    expected = os.getenv("INTERNAL_SWEEP_TOKEN")
    got = request.headers.get("x-internal-token")
    if not expected or not got or not secrets.compare_digest(got, expected):
        raise HTTPException(status_code=401, detail="unauthorized")
    n = sweep_overdue_tenants(db)
    db.commit()
    return {"suspended": n}


@router.post("/internal/commission-billing/run")
def commission_billing_run(request: Request, period: str | None = None, db: Session = Depends(get_global_db)):
    """Dispara o faturamento mensal da comissão medida do tenant (Fase 1).

    Protegido por INTERNAL_SWEEP_TOKEN (mesmo header/padrão do sweep do Projeto B).
    `period` = 'YYYY-MM'. Se omitido, usa o MÊS ANTERIOR no fuso America/Sao_Paulo
    (uso do cron mensal: roda no dia 1 e fatura o mês que acabou). Idempotente: só
    fatura entradas com status `accrued`.
    """
    import os, re, secrets
    from app.services.commission_billing_service import (
        run_monthly_commission_billing,
        make_asaas_charge_fn,
    )
    expected = os.getenv("INTERNAL_SWEEP_TOKEN")
    got = request.headers.get("x-internal-token")
    if not expected or not got or not secrets.compare_digest(got, expected):
        raise HTTPException(status_code=401, detail="unauthorized")
    if period is None:
        from datetime import timezone
        # BRT = UTC-3 fixo (Brasil aboliu horário de verão em 2019). Evita depender
        # de tzdata (ausente no python:3.13-slim e no Windows).
        today_sp = (datetime.now(timezone.utc) - timedelta(hours=3)).date()
        last_day_prev = today_sp.replace(day=1) - timedelta(days=1)
        period = last_day_prev.strftime("%Y-%m")
    if not re.fullmatch(r"\d{4}-\d{2}", period):
        raise HTTPException(status_code=422, detail="period must be YYYY-MM")
    ids = run_monthly_commission_billing(db, period, charge_fn=make_asaas_charge_fn())
    # run_monthly_commission_billing já comita por tenant individualmente;
    # db.commit() adicional aqui seria redundante.
    return {"period": period, "charges_created": len(ids)}


@router.post("/internal/credit-expiry/sweep")
def credit_expiry_sweep(request: Request, db: Session = Depends(get_global_db)):
    """Varre créditos expirados/cancelados e reconhece breakage contábil (Item 3 + 4).

    Protegido por INTERNAL_SWEEP_TOKEN. Chamado pelo Cloud Scheduler (recomendado: diário).
    Idempotente — re-execução não duplica entradas.
    CAMADA CONTÁBIL: não move dinheiro, não altera saldos de pagamento.
    """
    import os, secrets
    from app.services.credit_expiry_service import sweep_expired_credits
    expected = os.getenv("INTERNAL_SWEEP_TOKEN")
    got = request.headers.get("x-internal-token")
    if not expected or not got or not secrets.compare_digest(got, expected):
        raise HTTPException(status_code=401, detail="unauthorized")
    result = sweep_expired_credits(db)
    return result


@router.post("/internal/background-reverification/sweep")
def background_reverification_sweep(request: Request, db: Session = Depends(get_global_db)):
    """Expira certidoes de antecedentes validadas ha mais de N dias (reverificacao).

    Protegido por INTERNAL_SWEEP_TOKEN. Chamado pelo Cloud Scheduler (recomendado:
    diario). Idempotente — so age em tenants com a flag `background_checks` ON e so
    notifica na transicao validated->expired. Cron interno = fail-closed normal.
    """
    import os, secrets
    from app.services.background_reverification_service import sweep_stale_certificates
    expected = os.getenv("INTERNAL_SWEEP_TOKEN")
    got = request.headers.get("x-internal-token")
    if not expected or not got or not secrets.compare_digest(got, expected):
        raise HTTPException(status_code=401, detail="unauthorized")
    return sweep_stale_certificates(db)


@router.post("/internal/free-trial/sweep")
def free_trial_sweep(request: Request, db: Session = Depends(get_global_db)):
    """Carimba downgrades de reverse trial expirados + notifica admins (plano free).

    OPCIONAL: o enforcement econômico do fim do trial é STATELESS (plano efetivo
    resolvido a cada request) — este sweep só garante a NOTIFICAÇÃO de downgrade
    mesmo que o admin não abra o painel. Protegido por INTERNAL_SWEEP_TOKEN
    (mesmo padrão dos demais sweeps). Idempotente (carimbo trial_downgraded_at).
    """
    import os, secrets
    from datetime import datetime as _dt

    from app.models.tenant import Tenant as _Tenant
    from app.services.tenant_free_plan_service import maybe_downgrade_expired_trial

    expected = os.getenv("INTERNAL_SWEEP_TOKEN")
    got = request.headers.get("x-internal-token")
    if not expected or not got or not secrets.compare_digest(got, expected):
        raise HTTPException(status_code=401, detail="unauthorized")
    candidates = (
        db.query(_Tenant)
        .filter(
            _Tenant.plan == "free",
            _Tenant.trial_ends_at.isnot(None),
            _Tenant.trial_ends_at < _dt.utcnow(),
            _Tenant.trial_downgraded_at.is_(None),
        )
        .all()
    )
    n = 0
    for tenant in candidates:
        if maybe_downgrade_expired_trial(db, tenant):
            n += 1
    if n:
        db.commit()
    return {"downgraded": n}


def _is_tip_payment(db, provider_payment_id: str, external_ref: str) -> bool:
    """Verifica se um provider_payment_id pertence a uma WalkTip (fallback sem externalReference)."""
    from app.models.walk_tip import WalkTip
    return (
        db.query(WalkTip)
        .filter(WalkTip.provider_payment_id == provider_payment_id)
        .first()
    ) is not None


def _handle_nfse_webhook(db, event: str, invoice_data: dict) -> None:
    """Wrapper síncrono para handle_nfse_webhook_event do nfse_service.

    Delegação direta — o nfse_service é responsável por toda a lógica.
    Exceções propagam para o caller (asaas_webhook), que faz rollback + 500.
    """
    from app.services.nfse_service import handle_nfse_webhook_event
    handle_nfse_webhook_event(db, event, invoice_data)


def _provision_safe(db, tenant_id, payment_like, revenue_type) -> None:
    """Best-effort: registra a provisão fiscal do pagamento confirmado.
    NUNCA propaga exceção — provisão jamais pode quebrar o webhook de pagamento."""
    try:
        from app.services.provision_service import compute_and_store_provision
        compute_and_store_provision(db, tenant_id, payment_like, revenue_type)
    except Exception:
        logger.exception("provision: falha best-effort tenant_id=%s revenue_type=%s", tenant_id, revenue_type)
        try:
            db.rollback()
        except Exception:
            pass
