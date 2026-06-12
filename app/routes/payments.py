import os
import asyncio
import secrets
import logging
from datetime import date, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from dotenv import load_dotenv

from app.core.database import get_db
from app.dependencies.auth import get_current_user
from app.models.payment import Payment
from app.models.user import User
from app.schemas.payment import PaymentCreate, PaymentResponse
from app.services.payment_split_service import build_payment_split

router = APIRouter(prefix="/payments", tags=["payments"])
logger = logging.getLogger("app.routes.payments")

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

PAYMENT_MODE = os.getenv("PAYMENT_MODE", "asaas_sandbox")
ASAAS_SANDBOX_BASE_URL = os.getenv("ASAAS_SANDBOX_BASE_URL", "https://api-sandbox.asaas.com/v3").rstrip("/")
ASAAS_SANDBOX_API_KEY = os.getenv("ASAAS_SANDBOX_API_KEY") or os.getenv("ASAAS_API_KEY")
ASAAS_SANDBOX_DEFAULT_CPF_CNPJ = os.getenv("ASAAS_SANDBOX_DEFAULT_CPF_CNPJ", "24971563792")
SENSITIVE_KEYS = {"access_token", "authorization", "api_key", "token", "password"}

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


def asaas_headers():
    if not ASAAS_SANDBOX_API_KEY:
        raise HTTPException(status_code=503, detail="ASAAS_SANDBOX_API_KEY nao configurada para o sandbox.")

    return {
        "access_token": ASAAS_SANDBOX_API_KEY,
        "Content-Type": "application/json",
        "User-Agent": "Aumigao Beta Sandbox",
    }


def sanitize_for_log(value):
    if isinstance(value, dict):
        sanitized = {}
        for key, item in value.items():
            normalized_key = key.lower()
            if normalized_key in SENSITIVE_KEYS or "token" in normalized_key or "key" in normalized_key:
                sanitized[key] = "***"
            else:
                sanitized[key] = sanitize_for_log(item)
        return sanitized
    if isinstance(value, list):
        return [sanitize_for_log(item) for item in value]
    return value


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
    diagnostic = {
        "step": step,
        "status_http": response.status_code,
        "asaas_code": asaas_error["code"],
        "asaas_description": asaas_error["description"],
        "request_payload": sanitize_for_log(request_payload or {}),
        "asaas_response": sanitize_for_log(response_data),
    }
    logger.error("Asaas Sandbox error: %s", diagnostic)
    raise HTTPException(
        status_code=502,
        detail={
            "message": f"Falha Asaas Sandbox em {step}.",
            "status_http": response.status_code,
            "asaas_code": asaas_error["code"],
            "asaas_description": asaas_error["description"],
            "request_payload": sanitize_for_log(request_payload or {}),
            "asaas": sanitize_for_log(response_data),
        },
    )


def normalize_method(method: str):
    normalized = (method or "pix").strip().lower()
    return "UNDEFINED" if normalized in {"card", "credit_card", "cartao", "cartão"} else "PIX"


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
        "invoice_url": extra.get("invoice_url"),
        "pix_qr_code": extra.get("pix_qr_code"),
        "pix_copy_paste": extra.get("pix_copy_paste"),
        "pix_expiration_date": extra.get("pix_expiration_date"),
        "sandbox_message": extra.get("sandbox_message") or "Ambiente Sandbox: nenhuma cobranca real sera realizada.",
        "commission_percent": payment.commission_percent,
        "platform_amount": payment.platform_amount,
        "walker_amount": payment.walker_amount,
        "created_at": payment.created_at,
    }


async def create_asaas_customer(client: httpx.AsyncClient, user: User):
    payload = {
        "name": user.full_name or user.email,
        "email": user.email,
        "cpfCnpj": ASAAS_SANDBOX_DEFAULT_CPF_CNPJ,
        "externalReference": user.id,
        "notificationDisabled": True,
    }
    logger.warning("Asaas Sandbox request customers payload=%s", sanitize_for_log(payload))
    response = await client.post("/customers", json=payload)
    if response.status_code >= 400:
        raise_asaas_error("customers.create", response, payload)
    data = response.json()
    logger.warning("Asaas Sandbox response customers status_http=%s customer_id=%s", response.status_code, data.get("id"))
    return data["id"]


async def create_asaas_payment(payload: PaymentCreate, user: User):
    billing_type = normalize_method(payload.method)
    async with httpx.AsyncClient(base_url=ASAAS_SANDBOX_BASE_URL, headers=asaas_headers(), timeout=20) as client:
        customer_id = await create_asaas_customer(client, user)
        payment_payload = {
            "customer": customer_id,
            "billingType": billing_type,
            "value": payload.amount,
            "dueDate": str(date.today() + timedelta(days=1)),
            "description": "Passeio Aumigao - Beta Fechado",
            "externalReference": payload.walk_id or str(uuid4()),
        }
        logger.warning("Asaas Sandbox request payments payload=%s", sanitize_for_log(payment_payload))
        response = await client.post("/payments", json=payment_payload)
        if response.status_code >= 400:
            raise_asaas_error("payments.create", response, payment_payload)

        payment_data = response.json()
        logger.warning(
            "Asaas Sandbox response payments status_http=%s payment_id=%s provider_status=%s billing_type=%s",
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
                    "Asaas Sandbox pix_qr_code retry attempt=%s status_http=%s payment_id=%s",
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
            logger.warning(
                "Asaas Sandbox response pix_qr_code status_http=%s payment_id=%s has_payload=%s",
                pix_response.status_code,
                payment_data.get("id"),
                bool(pix_data.get("payload")),
            )

        return payment_data, pix_data, billing_type


PAYMENT_PENDING_STATUSES = {
    "pagamento_sandbox_criado",
    "aguardando_pagamento",
}

@router.post("/create", response_model=PaymentResponse)
async def create_payment(payload: PaymentCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if PAYMENT_MODE != "asaas_sandbox":
        raise HTTPException(status_code=400, detail="PAYMENT_MODE deve ser asaas_sandbox no beta fechado.")

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

    try:
      provider_data, pix_data, _billing_type = await create_asaas_payment(payload, user)
      provider_status = provider_data.get("status")
    except Exception as error:
      logger.warning("Asaas Sandbox indisponivel; usando fallback interno beta. error=%s", error)
      provider_data = {
        "id": f"internal-sandbox-{uuid4()}",
        "status": "PAYMENT_CREATED",
        "invoiceUrl": None,
        "bankSlipUrl": None,
      }
      pix_data = {}
      provider_status = provider_data.get("status")
    split = build_payment_split(db, user.tenant_id, payload.amount)
    payment = Payment(
        id=str(uuid4()),
        tenant_id=user.tenant_id,
        tutor_id=user.id,
        walk_id=payload.walk_id,
        amount=payload.amount,
        status=normalize_payment_status(provider_status),
        provider="asaas_sandbox",
        provider_payment_id=provider_data.get("id"),
        commission_percent=split["commission_percent"],
        platform_amount=split["platform_amount"],
        walker_amount=split["walker_amount"],
    )
    db.add(payment)
    db.commit()
    db.refresh(payment)
    return payment_response(
        payment,
        method=payload.method,
        provider_status=provider_status,
        invoice_url=provider_data.get("invoiceUrl") or provider_data.get("bankSlipUrl"),
        pix_qr_code=pix_data.get("encodedImage"),
        pix_copy_paste=pix_data.get("payload"),
        pix_expiration_date=pix_data.get("expirationDate"),
        sandbox_message="Cobranca criada no Asaas Sandbox. Nenhuma cobranca real sera realizada.",
    )


@router.get("/{payment_id}", response_model=PaymentResponse)
def get_payment(payment_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    payment = db.get(Payment, payment_id)
    # Retorna 404 (e nao 403) quando o pagamento nao e do solicitante para nao
    # revelar a existencia de pagamentos de outros usuarios via enumeracao de ID.
    is_admin = user.role in {"admin", "super_admin"}
    if not payment or (payment.tutor_id != user.id and not is_admin):
        raise HTTPException(status_code=404, detail="Pagamento nao encontrado.")
    return payment_response(payment)


@router.post("/webhooks/asaas")
def asaas_webhook(request: Request, payload: dict, db: Session = Depends(get_db)):
    expected = os.getenv("ASAAS_WEBHOOK_TOKEN")
    received = request.headers.get("asaas-access-token")

    if not expected or not secrets.compare_digest(expected, received or ""):
        raise HTTPException(status_code=401, detail="Webhook não autorizado")

    event = payload.get("event")
    provider_payment_id = (payload.get("payment") or {}).get("id")
    if provider_payment_id:
        payment = db.query(Payment).filter(Payment.provider_payment_id == provider_payment_id).first()
        if payment:
            payment.status = STATUS_BY_WEBHOOK_EVENT.get(event, normalize_payment_status((payload.get("payment") or {}).get("status")))
            db.add(payment)
            db.commit()
    return {"ok": True, "received": event}
