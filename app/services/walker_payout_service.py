"""Estorno (void) e PIX automático do ganho do passeador (Fase 3).

void = remove o ganho do saldo (não paga ninguém). transfer = move dinheiro real
(gated por WALKER_AUTO_PIX_ENABLED). Princípio: falha-fechada, idempotente.
"""
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.walker_earning import WalkerEarning, WE_VOID


def void_walker_earning(db: Session, walk_id: str, *, reason: str, source: str) -> WalkerEarning | None:
    """Anula (idempotente) o ganho do passeador de um passeio. Retorna a entrada ou None.

    Só age sobre entradas ainda não anuladas. Não faz commit (caller comita).
    Observação: se o ganho já tiver sido sacado, o saldo pode ficar negativo
    (clawback legítimo — o passeador deve o valor de um passeio revertido).
    """
    earning = db.query(WalkerEarning).filter(WalkerEarning.walk_id == walk_id).first()
    if earning is None or earning.status == WE_VOID:
        return None
    earning.status = WE_VOID
    earning.void_reason = reason
    earning.voided_at = datetime.now(timezone.utc)
    return earning
