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
        # sandbox_message: usa sentinela _UNSET para distinguir None explícito (live) de ausente.
        # Quando o caller passa sandbox_message=None (modo live), o campo fica None no JSON.
        # Quando não passa nada (ex: get_payment), cai no default apenas se sandbox-mode.
        "sandbox_message": (
            extra["sandbox_message"]
            if "sandbox_message" in extra
            else "Ambiente Sandbox: nenhuma cobranca real sera realizada."
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
        payment_payload = {
            "customer": customer_id,
            "billingType": billing_type,
            "value": payload.amount,
            "dueDate": str(date.today() + timedelta(days=1)),
            "description": "Passeio Aumigao",
            "externalReference": payload.walk_id or str(uuid4()),
        }

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

    # Idempotencia: se ja existe um pagamento em aberto para este walk_id,
    # devolve o existente sem criar novo no Asaas.
    if payload.walk_id:
        existing = (
            db.query(Payment)
            .filter(
                Payment.walk_id == payload.walk_id,
                Payment.status.in_(PAYMENT_PENDING_STATUSES),
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


@router.get("/{payment_id}", response_model=PaymentResponse)
def get_payment(payment_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    payment = db.get(Payment, payment_id)
    # Retorna 404 (e nao 403) quando o pagamento nao e do solicitante para nao
    # revelar a existencia de pagamentos de outros usuarios via enumeracao de ID.
    is_admin = user.role in {"admin", "super_admin"}
    if not payment or (payment.tutor_id != user.id and not is_admin):
        raise HTTPException(status_code=404, detail="Pagamento nao encontrado.")
    return payment_response(payment)


_PAYMENT_CONFIRMED_STATUS = "pagamento_confirmado_sandbox"
_PAYMENT_CONFIRMED_EVENTS = {"PAYMENT_CONFIRMED", "PAYMENT_RECEIVED", "PAYMENT_DUNNING_RECEIVED"}

# R3 — estado de estorno consumado, DISTINTO de 'falha_pagamento' (que é falha de
# cobrança). Auditável: um pagamento confirmado que foi estornado não deve ser
# confundido com uma cobrança que nunca liquidou.
_PAYMENT_REFUNDED_STATUS = "pagamento_estornado"
# Eventos que consumam o estorno e tiram o pagamento do estado confirmado.
_PAYMENT_REFUND_EVENTS = {"PAYMENT_REFUNDED"}
# Estados terminais "pegajosos": uma vez aqui, eventos de cobrança comuns NÃO
# regridem o status (só um estorno consumado pode sair de confirmado).
_PAYMENT_STICKY_STATUSES = {_PAYMENT_CONFIRMED_STATUS, _PAYMENT_REFUNDED_STATUS}


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
    return True


def _handle_subscription_webhook(db, event: str, payment_data: dict) -> bool:
    """Processa webhooks de cobranças geradas por subscriptions Asaas.

    Cria/atualiza Payment local vinculado ao tutor para aparecer no financeiro
    e notifica o tutor na confirmação.
    Retorna True se tratou, False caso contrário.
    """
    external_ref = payment_data.get("externalReference") or ""
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

    event = payload.get("event")
    payment_data = payload.get("payment") or {}
    provider_payment_id = payment_data.get("id")
    external_ref = payment_data.get("externalReference") or ""

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
    # Não existe tabela de dedup persistente por event-id (seria uma migration).
    # As funções _handle_tip_webhook / _handle_subscription_webhook já são
    # idempotentes por design (upsert baseado em provider_payment_id / localização
    # de registro existente), mas sem uma chave de evento gravada não há garantia
    # 100% contra reenvio duplo em janela de falha parcial.
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
            raise HTTPException(status_code=500, detail="Erro interno ao processar webhook de gorjeta.")
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
                if new_status == _PAYMENT_CONFIRMED_STATUS and payment.walk_id:
                    walk = db.get(Walk, payment.walk_id)
                    if walk and getattr(walk, "operational_status", None) == "awaiting_payment":
                        walk.operational_status = "pending_walker_confirmation"
                        walk.status = "Agendado"
                        db.add(walk)
                        logger.info("asaas_webhook.walk_liberado walk_id=%s payment_id=%s", walk.id, payment.id)

                db.commit()
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
        raise HTTPException(status_code=500, detail="Erro interno ao processar webhook de pagamento.")
    return {"ok": True, "received": event}


def _is_tip_payment(db, provider_payment_id: str, external_ref: str) -> bool:
    """Verifica se um provider_payment_id pertence a uma WalkTip (fallback sem externalReference)."""
    from app.models.walk_tip import WalkTip
    return (
        db.query(WalkTip)
        .filter(WalkTip.provider_payment_id == provider_payment_id)
        .first()
    ) is not None
