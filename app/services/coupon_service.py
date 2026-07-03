"""Regras de negócio dos cupons (Onda 2 — monetização).

Gated pela feature flag por tenant `coupons`. Antifraude: limite total de usos e
limite por usuário (via registro de resgates). O desconto é definido pelo tenant.
"""
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.coupon import (
    COUPONS_FEATURE_KEY,
    DISCOUNT_FIXED,
    DISCOUNT_PERCENT,
    Coupon,
    CouponRedemption,
)
from app.models.tenant import Tenant
from app.services.tenant_plan_service import enforce_tenant_product_feature, tenant_has_feature

FEATURE_LABEL = "Cupons"


def coupons_enabled(tenant: Tenant, db: Session) -> bool:
    return tenant_has_feature(tenant, db, COUPONS_FEATURE_KEY)


def enforce_enabled(tenant: Tenant, db: Session) -> None:
    enforce_tenant_product_feature(tenant, db, COUPONS_FEATURE_KEY, FEATURE_LABEL)


def _normalize(code: str) -> str:
    return (code or "").strip().upper()


def get_by_code(db: Session, tenant_id: str, code: str) -> Coupon | None:
    return (
        db.query(Coupon)
        .filter(Coupon.tenant_id == tenant_id, Coupon.code == _normalize(code))
        .first()
    )


def compute_discount(coupon: Coupon, amount: float) -> float:
    if coupon.discount_type == DISCOUNT_PERCENT:
        disc = amount * (coupon.discount_value / 100.0)
    else:  # fixed
        disc = coupon.discount_value
    # Nunca desconta mais que o valor; nunca negativo.
    return round(max(0.0, min(disc, amount)), 2)


def validate(db: Session, tenant: Tenant, code: str, user_id: str, amount: float) -> dict:
    """Avalia um cupom para um usuário/valor SEM resgatar. Retorna dict de resultado
    (valid/discount/message) — não levanta exceção, para o preview no checkout."""
    norm = _normalize(code)
    fail = lambda msg: {"valid": False, "code": norm, "discount_amount": 0.0, "final_amount": round(amount, 2), "message": msg}

    coupon = get_by_code(db, tenant.id, norm)
    if not coupon or not coupon.active:
        return fail("Cupom inválido.")
    now = datetime.utcnow()
    if coupon.valid_from and now < coupon.valid_from:
        return fail("Cupom ainda não está válido.")
    if coupon.valid_until and now > coupon.valid_until:
        return fail("Cupom expirado.")
    if amount < coupon.min_amount:
        return fail(f"Valor mínimo de {coupon.min_amount:.2f} para usar este cupom.")
    if coupon.max_uses is not None and coupon.uses_count >= coupon.max_uses:
        return fail("Cupom esgotado.")
    if coupon.max_uses_per_user is not None:
        used_by_user = (
            db.query(CouponRedemption)
            .filter(CouponRedemption.coupon_id == coupon.id, CouponRedemption.user_id == user_id)
            .count()
        )
        if used_by_user >= coupon.max_uses_per_user:
            return fail("Você já usou este cupom.")

    discount = compute_discount(coupon, amount)
    return {
        "valid": True,
        "code": norm,
        "discount_amount": discount,
        "final_amount": round(amount - discount, 2),
        "message": "Cupom aplicado.",
    }


def redeem(db: Session, tenant: Tenant, code: str, user_id: str, amount: float, walk_id: str | None = None) -> CouponRedemption:
    """Resgata o cupom (no checkout): revalida, registra o resgate e incrementa usos."""
    enforce_enabled(tenant, db)
    result = validate(db, tenant, code, user_id, amount)
    if not result["valid"]:
        raise HTTPException(status_code=409, detail=result["message"])
    coupon = get_by_code(db, tenant.id, code)

    # Anti-race: consumo atômico do contador de usos. Sob concorrência, dois
    # redeems simultâneos de um cupom max_uses=1 poderiam ambos passar pela
    # validate() (leitura) e incrementar em memória (uses_count += 1), gerando
    # resgate duplo. O UPDATE condicional garante que só UMA transação vence:
    # a segunda não afeta linha nenhuma (uses_count já >= max_uses) e é rejeitada.
    if coupon.max_uses is not None:
        updated = db.execute(
            text(
                "UPDATE coupons SET uses_count = uses_count + 1, updated_at = :now "
                "WHERE id = :id AND uses_count < :max_uses"
            ),
            {"now": datetime.utcnow(), "id": coupon.id, "max_uses": coupon.max_uses},
        ).rowcount
        if not updated:
            db.rollback()
            raise HTTPException(status_code=409, detail="Cupom esgotado.")
    else:
        # Sem teto total: incremento atômico simples (sem condição de esgotamento).
        db.execute(
            text(
                "UPDATE coupons SET uses_count = uses_count + 1, updated_at = :now "
                "WHERE id = :id"
            ),
            {"now": datetime.utcnow(), "id": coupon.id},
        )

    redemption = CouponRedemption(
        coupon_id=coupon.id,
        tenant_id=tenant.id,
        user_id=user_id,
        walk_id=walk_id,
        amount_discounted=result["discount_amount"],
        single_use_per_user=(coupon.max_uses_per_user == 1),
    )
    db.add(redemption)
    if walk_id:
        from app.models.walk import Walk
        _walk = db.get(Walk, walk_id)
        if _walk is not None and _walk.tenant_id == tenant.id:   # guard cross-tenant
            if getattr(coupon, "is_referral_gift", False):
                _walk.is_referral_gift = True
            # R7: cupom que cobre 100% do valor do passeio faz o papel do pagamento —
            # promove o walk de 'awaiting_payment' para o fluxo operacional (matching),
            # espelhando o webhook de pagamento confirmado (payments.py). Cupom PARCIAL
            # NÃO promove: o pagamento do restante o libera via webhook.
            # amount = preço do walk (vem da rota de redeem); discount_amount é limitado
            # a amount em compute_discount, então >= amount só quando cobre 100%.
            _covers_full = amount > 0 and result["discount_amount"] >= amount
            if _covers_full and getattr(_walk, "operational_status", None) == "awaiting_payment":
                _walk.operational_status = "pending_walker_confirmation"
                _walk.status = "Agendado"
                _walk.no_walker_reason = "Buscando o melhor passeador disponível."
    try:
        db.commit()
    except IntegrityError:
        # Unique index parcial (coupon_id, user_id) quando há limite por usuário:
        # defesa de banco contra double-grant do mesmo user sob concorrência.
        db.rollback()
        raise HTTPException(status_code=409, detail="Você já usou este cupom.")
    db.refresh(redemption)
    return redemption


# ----- Admin (catálogo) -----
def list_coupons(db: Session, tenant_id: str) -> list[Coupon]:
    return db.query(Coupon).filter(Coupon.tenant_id == tenant_id).order_by(Coupon.created_at.desc()).all()


def get_or_404(db: Session, tenant_id: str, coupon_id: str) -> Coupon:
    coupon = db.query(Coupon).filter(Coupon.tenant_id == tenant_id, Coupon.id == coupon_id).first()
    if not coupon:
        raise HTTPException(status_code=404, detail="Cupom não encontrado.")
    return coupon
