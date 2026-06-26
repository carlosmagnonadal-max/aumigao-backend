"""Backfill de payment_provision para pagamentos confirmados sem provisão.

Uso (prod, via owner): DATABASE_URL=<dono> ./.venv/Scripts/python.exe scripts/backfill_provisions.py
Idempotente: compute_and_store_provision pula pagamentos já provisionados.
"""
import logging
from sqlalchemy.orm import Session
from app.models.payment import Payment
from app.services.provision_service import compute_and_store_provision

logger = logging.getLogger("aumigao.backfill_provisions")
_CONFIRMED = "pagamento_confirmado_sandbox"

def _revenue_type_for(payment: Payment) -> str:
    # heurística: walk_id presente -> comissão de passeio; senão saas/avulso.
    return "walk_commission" if getattr(payment, "walk_id", None) else "saas_subscription"

def backfill_provisions(db: Session) -> int:
    n = 0
    rows = db.query(Payment).filter(Payment.status == _CONFIRMED).all()
    for p in rows:
        tenant_id = getattr(p, "tenant_id", None)
        if not tenant_id:
            continue
        compute_and_store_provision(db, tenant_id, p, _revenue_type_for(p))
        n += 1
    return n

if __name__ == "__main__":
    from app.core.database import SessionLocal
    db = SessionLocal()
    created = backfill_provisions(db)
    db.close()
    print(f"backfill: processados {created} pagamentos confirmados")
