"""Estorno (void) e PIX automático do ganho do passeador (Fase 3).

void = remove o ganho do saldo (não paga ninguém). transfer = move dinheiro real
(gated por WALKER_AUTO_PIX_ENABLED). Princípio: falha-fechada, idempotente.
"""
import logging
import os
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.payment import Payment
from app.models.walker_earning import WalkerEarning, WE_VOID
from app.models.walker_profile import WalkerProfile

logger = logging.getLogger(__name__)

# Status de saque PIX que indicam que o dinheiro JÁ saiu (clawback = PIX a recuperar).
_PAID_WITHDRAWAL_STATUSES = {"paid", "processing"}


def _walker_has_paid_withdrawal(db: Session, walker_id: str) -> bool:
    """True se o passeador já teve algum saque PIX pago/em processamento — sinaliza
    que anular um ganho agora é um clawback (dinheiro que já saiu, "PIX a recuperar")."""
    return (
        db.query(Payment)
        .filter(
            Payment.tutor_id == walker_id,  # no saque, tutor_id guarda o walker
            Payment.provider == "pix",
            Payment.walk_id.is_(None),
            Payment.amount < 0,
            Payment.status.in_(_PAID_WITHDRAWAL_STATUSES),
        )
        .first()
        is not None
    )


def _notify_admins_clawback(db: Session, earning: WalkerEarning, *, reason: str, source: str) -> None:
    """Alerta os admins do tenant que há um PIX a recuperar (clawback pós-pagamento).
    Best-effort: nunca bloqueia o void."""
    try:
        from app.models.user import User
        from app.routes.notifications import NotificationCreate, _create_notification

        admins = (
            db.query(User)
            .filter(
                User.role.in_(["admin", "super_admin"]),
                (User.tenant_id == earning.tenant_id) if earning.tenant_id else (User.tenant_id.isnot(None)),
            )
            .all()
            if earning.tenant_id
            else db.query(User).filter(User.role.in_(["admin", "super_admin"])).all()
        )
        value = abs(float(earning.amount or 0))
        for admin in admins:
            _create_notification(db, NotificationCreate(
                user_id=admin.id,
                user_role=admin.role,
                tenant_id=earning.tenant_id,
                title="⚠️ PIX a recuperar (estorno pós-pagamento)",
                message=(
                    f"O ganho de R$ {value:.2f} do passeador foi anulado ({reason}), "
                    "mas o passeador já tinha saques PIX pagos. Verifique recuperação do valor."
                ),
                type="walker_payout_clawback",
                related_entity_type="walker_earning",
                related_entity_id=earning.id,
                metadata={"walk_id": earning.walk_id, "amount": value, "reason": reason, "source": source},
            ))
    except Exception:
        logger.exception(
            "void_walker_earning: falha best-effort ao notificar admins de clawback walk_id=%s",
            earning.walk_id,
        )


def void_walker_earning(db: Session, walk_id: str, *, reason: str, source: str) -> WalkerEarning | None:
    """Anula (idempotente) o ganho do passeador de um passeio. Retorna a entrada ou None.

    Só age sobre entradas ainda não anuladas. Não faz commit (caller comita).
    Observação: se o ganho já tiver sido sacado, o saldo pode ficar negativo
    (clawback legítimo — o passeador deve o valor de um passeio revertido).

    Clawback pós-PIX (P2): se o passeador já teve saque PIX pago, o dinheiro saiu.
    Anular o ganho não bloqueia (segue o void), mas emitimos alerta estruturado +
    notificação aos admins sinalizando "PIX a recuperar".
    """
    earning = db.query(WalkerEarning).filter(WalkerEarning.walk_id == walk_id).first()
    if earning is None or earning.status == WE_VOID:
        return None
    earning.status = WE_VOID
    earning.void_reason = reason
    earning.voided_at = datetime.now(timezone.utc)

    if _walker_has_paid_withdrawal(db, earning.walker_id):
        logger.warning(
            "walker_payout.clawback_after_pix walk_id=%s walker_id=%s tenant_id=%s amount=%s reason=%s source=%s",
            earning.walk_id, earning.walker_id, earning.tenant_id, earning.amount, reason, source,
        )
        _notify_admins_clawback(db, earning, reason=reason, source=source)

    return earning


# ---------------------------------------------------------------------------
# PIX automático (Fase 3) — gated por WALKER_AUTO_PIX_ENABLED (OFF por padrão)
# ---------------------------------------------------------------------------

def _auto_pix_enabled() -> bool:
    """Lê a flag em RUNTIME (via os.getenv) para que monkeypatch.setenv funcione nos testes."""
    return os.getenv("WALKER_AUTO_PIX_ENABLED", "false").lower() in {"1", "true", "yes"}


def _asaas_transfer_post(value: float, pix_key: str) -> str:
    """Cria uma transferência PIX no Asaas e retorna o id da transferência.

    Reusa _get_asaas_config() de payments.py (mesmo padrão de autenticação).
    Mockado nos testes; chamado de verdade apenas com a flag ligada em produção.

    Campos do POST /transfers (Asaas):
      - value: float (valor em reais, arredondado para 2 casas)
      - pixAddressKey: str (chave PIX do recebedor)
      - operationType: "PIX" (discriminador de tipo de transferência)
    """
    import httpx
    from app.routes.payments import _get_asaas_config
    cfg = _get_asaas_config()
    payload = {
        "value": round(float(value), 2),
        "pixAddressKey": pix_key,
        "operationType": "PIX",
    }
    with httpx.Client(
        base_url=cfg["base_url"],
        headers={"access_token": cfg["api_key"], "Content-Type": "application/json"},
        timeout=20,
    ) as client:
        resp = client.post("/transfers", json=payload)
        if resp.status_code >= 400:
            raise HTTPException(
                status_code=502,
                detail="Falha na transferencia PIX ao passeador.",
            )
        tid = resp.json().get("id")
        if not tid:
            raise HTTPException(
                status_code=502,
                detail="Asaas retornou resposta sem id de transferencia.",
            )
        return tid


def transfer_to_walker(db: Session, payment: Payment) -> str | None:
    """Transfere o valor do saque para a chave PIX do passeador (se a flag estiver ON).

    Comportamentos:
    - Flag OFF  => retorna None (no-op; mantém fluxo manual).
    - Já transferido (provider_payment_id setado) => retorna o id sem nova chamada (idempotente).
    - Sem chave PIX => levanta HTTPException 400.
    - Falha no Asaas => _asaas_transfer_post levanta HTTPException 502.

    Não faz commit (caller comita). Se levantar exceção, o caller NÃO comita
    (falha-fechada: o status "paid" não persiste se o PIX falhar).
    """
    if not _auto_pix_enabled():
        return None

    if payment.provider_payment_id:
        # Já foi transferido anteriormente — idempotente.
        return payment.provider_payment_id

    # tutor_id == walker.id no Payment de saque (ver walker.py:687)
    profile = db.query(WalkerProfile).filter(WalkerProfile.user_id == payment.tutor_id).first()
    pix_key = profile.pix_key if profile else None
    if not pix_key:
        raise HTTPException(status_code=400, detail="Passeador sem chave PIX cadastrada.")

    value = abs(float(payment.amount or 0))
    transfer_id = _asaas_transfer_post(value, pix_key)
    payment.provider_payment_id = transfer_id
    return transfer_id
