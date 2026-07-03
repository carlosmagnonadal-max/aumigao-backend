"""R7 — cupom que cobre 100% do valor promove o walk de 'awaiting_payment'.

Com o gate de pagamento ligado, o passeio pago por cupom integral não gera cobrança
no backend; o resgate server-side (coupon_service.redeem) faz o papel do webhook e
libera o walk para o fluxo operacional. Cupom PARCIAL não promove — o pagamento do
restante libera via webhook.
"""
from __future__ import annotations

import app.models  # noqa: F401

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.models.coupon import Coupon
from app.models.tenant import Tenant, TenantFeature
from app.models.walk import Walk
from app.services import coupon_service as cs


def _db(discount_value: float, discount_type: str = "percent"):
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    db.add(Tenant(id="t1", name="T1", slug="t1", status="active", plan="business"))
    db.add(TenantFeature(tenant_id="t1", feature_key="coupons", enabled=True))
    db.add(Walk(id="w1", tenant_id="t1", tutor_id="u2", price=50.0,
                status="aguardando_pagamento", operational_status="awaiting_payment",
                pet_id="p1", scheduled_date="2026-07-01", duration_minutes=30))
    db.add(Coupon(id="c1", tenant_id="t1", code="FULL", discount_type=discount_type,
                  discount_value=discount_value, max_uses=1, max_uses_per_user=1, active=True))
    db.commit()
    return db


def test_full_coupon_promotes_awaiting_walk():
    db = _db(100.0)  # 100%
    tenant = db.get(Tenant, "t1")
    cs.redeem(db, tenant, "FULL", user_id="u2", amount=50.0, walk_id="w1")
    walk = db.get(Walk, "w1")
    assert walk.operational_status == "pending_walker_confirmation"
    assert walk.status == "Agendado"


def test_full_fixed_coupon_covering_price_promotes():
    db = _db(50.0, discount_type="fixed")  # R$50 fixo cobre o walk de R$50
    tenant = db.get(Tenant, "t1")
    cs.redeem(db, tenant, "FULL", user_id="u2", amount=50.0, walk_id="w1")
    walk = db.get(Walk, "w1")
    assert walk.operational_status == "pending_walker_confirmation"


def test_partial_coupon_does_not_promote():
    db = _db(50.0)  # 50% — cobre metade
    tenant = db.get(Tenant, "t1")
    cs.redeem(db, tenant, "FULL", user_id="u2", amount=50.0, walk_id="w1")
    walk = db.get(Walk, "w1")
    # Cupom parcial NÃO libera: o pagamento do restante promove via webhook.
    assert walk.operational_status == "awaiting_payment"
    assert walk.status == "aguardando_pagamento"


def test_full_coupon_noop_when_walk_not_awaiting():
    db = _db(100.0)
    walk = db.get(Walk, "w1")
    walk.operational_status = "ride_in_progress"
    walk.status = "Passeando agora"
    db.commit()
    tenant = db.get(Tenant, "t1")
    cs.redeem(db, tenant, "FULL", user_id="u2", amount=50.0, walk_id="w1")
    walk = db.get(Walk, "w1")
    # Não estava à espera → não é rebaixado nem alterado.
    assert walk.operational_status == "ride_in_progress"
