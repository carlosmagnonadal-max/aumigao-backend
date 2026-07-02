"""FIX 1 (P1) — cupom race: dois resgates concorrentes de um cupom de uso único
não podem ambos ter sucesso (double-grant).

O teste roda em SQLite (sem locking real de linha), então simula a race no nível
de aplicação: duas SESSÕES independentes sobre o MESMO banco leem o cupom (ambas
passam pela validate) e tentam resgatar. A defesa é dupla:
  - UPDATE atômico condicional em uses_count (teto global);
  - índice único parcial (coupon_id, user_id) WHERE single_use_per_user (por usuário).
Só UM resgate deve vencer.
"""
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.models.coupon import Coupon, CouponRedemption
from app.models.tenant import Tenant, TenantFeature
from app.models.user import User
from app.models.walk import Walk
from app.services import coupon_service as svc
from app.services.tenant_seed_service import DEFAULT_TENANT_SLUG

TENANT_ID = "t1"


def _engine():
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(
        eng,
        tables=[
            Tenant.__table__,
            TenantFeature.__table__,
            Coupon.__table__,
            CouponRedemption.__table__,
            User.__table__,
            Walk.__table__,
        ],
    )
    return eng


def _seed(db, *, max_uses=1, max_uses_per_user=1, users=("u1", "u2")):
    db.add(Tenant(id=TENANT_ID, name="Aumigao", slug=DEFAULT_TENANT_SLUG, status="active", plan="business"))
    for u in users:
        db.add(User(id=u, email=f"{u}@t.com", password_hash="x", role="cliente", tenant_id=TENANT_ID))
    db.add(TenantFeature(tenant_id=TENANT_ID, feature_key="coupons", enabled=True))
    db.add(Coupon(
        tenant_id=TENANT_ID, code="PROMO10", discount_type="percent", discount_value=10,
        active=True, max_uses=max_uses, max_uses_per_user=max_uses_per_user,
    ))
    db.commit()


def test_concurrent_redeem_global_cap_only_one_wins():
    eng = _engine()
    Session = sessionmaker(bind=eng)
    setup = Session(); _seed(setup, max_uses=1); t = setup.get(Tenant, TENANT_ID); setup.close()

    db_a = Session(); db_b = Session()
    t_a = db_a.get(Tenant, TENANT_ID); t_b = db_b.get(Tenant, TENANT_ID)

    # Ambas as sessões validam ANTES de qualquer resgate (leem uses_count=0).
    assert svc.validate(db_a, t_a, "PROMO10", "u1", 100)["valid"] is True
    assert svc.validate(db_b, t_b, "PROMO10", "u2", 100)["valid"] is True

    # Primeiro resgate vence.
    svc.redeem(db_a, t_a, "PROMO10", "u1", 100)
    # Segundo resgate (usuário diferente) deve ser rejeitado: cupom esgotado.
    with pytest.raises(HTTPException) as e:
        svc.redeem(db_b, t_b, "PROMO10", "u2", 100)
    assert e.value.status_code == 409

    verify = Session()
    assert verify.query(CouponRedemption).count() == 1
    assert verify.get(Coupon, verify.query(Coupon).first().id).uses_count == 1


def test_concurrent_redeem_same_user_partial_unique_index_blocks_double_grant():
    # max_uses alto (teto global não protege), mas 1 por usuário. Duas sessões do
    # MESMO usuário: o índice único parcial deve barrar o segundo resgate.
    eng = _engine()
    Session = sessionmaker(bind=eng)
    setup = Session(); _seed(setup, max_uses=100, max_uses_per_user=1, users=("u1",)); setup.close()

    db_a = Session(); db_b = Session()
    t_a = db_a.get(Tenant, TENANT_ID); t_b = db_b.get(Tenant, TENANT_ID)

    assert svc.validate(db_a, t_a, "PROMO10", "u1", 100)["valid"] is True
    assert svc.validate(db_b, t_b, "PROMO10", "u1", 100)["valid"] is True

    svc.redeem(db_a, t_a, "PROMO10", "u1", 100)
    with pytest.raises(HTTPException) as e:
        svc.redeem(db_b, t_b, "PROMO10", "u1", 100)
    assert e.value.status_code == 409

    verify = Session()
    assert verify.query(CouponRedemption).filter_by(user_id="u1").count() == 1
