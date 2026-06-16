"""Comissão por plano (12/8/5) + override manual (commission_is_custom)."""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 - registra tabelas no Base.metadata
from app.core.database import Base
from app.models.tenant import Tenant
from app.models.tenant_payment_config import commission_default_for_plan
from app.services.payment_split_service import (
    compute_split,
    get_commission_percent,
    get_or_create_payment_config,
    update_payment_config,
)


def _db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _tenant(db, tid, plan):
    db.add(Tenant(id=tid, name=tid, slug=tid, status="active", plan=plan))
    db.commit()


def test_commission_default_for_plan():
    assert commission_default_for_plan("starter") == 12.0
    assert commission_default_for_plan("business") == 8.0
    assert commission_default_for_plan("enterprise") == 5.0
    assert commission_default_for_plan("desconhecido") == 10.0
    assert commission_default_for_plan(None) == 10.0


def test_new_config_uses_plan_default():
    db = _db()
    _tenant(db, "t-st", "starter")
    _tenant(db, "t-bz", "business")
    _tenant(db, "t-ent", "enterprise")
    assert get_or_create_payment_config(db, "t-st").commission_percent == 12.0
    assert get_or_create_payment_config(db, "t-bz").commission_percent == 8.0
    assert get_or_create_payment_config(db, "t-ent").commission_percent == 5.0


def test_manual_edit_marks_custom_and_persists():
    db = _db()
    _tenant(db, "t1", "business")
    cfg = get_or_create_payment_config(db, "t1")  # 8% (business)
    assert cfg.commission_is_custom is False
    update_payment_config(db, "t1", commission_percent=0.0)  # Fundador/sócio 0%
    cfg = get_or_create_payment_config(db, "t1")
    assert cfg.commission_percent == 0.0
    assert cfg.commission_is_custom is True


def test_saving_same_value_does_not_mark_custom():
    db = _db()
    _tenant(db, "t2", "business")
    get_or_create_payment_config(db, "t2")  # 8%
    update_payment_config(db, "t2", commission_percent=8.0)  # mesmo valor
    assert get_or_create_payment_config(db, "t2").commission_is_custom is False


def test_plan_change_updates_commission_when_not_custom():
    db = _db()
    t = Tenant(id="t3", name="t3", slug="t3", status="active", plan="starter")
    db.add(t)
    db.commit()
    cfg = get_or_create_payment_config(db, "t3")  # 12% (starter)
    assert cfg.commission_percent == 12.0
    # upgrade starter -> business (lógica do route update_tenant)
    t.plan = "business"
    if not cfg.commission_is_custom:
        cfg.commission_percent = commission_default_for_plan(t.plan)
    db.commit()
    assert get_or_create_payment_config(db, "t3").commission_percent == 8.0


def test_plan_change_preserves_custom():
    db = _db()
    t = Tenant(id="t4", name="t4", slug="t4", status="active", plan="starter")
    db.add(t)
    db.commit()
    get_or_create_payment_config(db, "t4")
    update_payment_config(db, "t4", commission_percent=0.0)  # custom (0%)
    cfg = get_or_create_payment_config(db, "t4")
    t.plan = "enterprise"
    if not cfg.commission_is_custom:
        cfg.commission_percent = commission_default_for_plan(t.plan)
    db.commit()
    assert get_or_create_payment_config(db, "t4").commission_percent == 0.0  # preservado


def test_compute_split_10_percent():
    s = compute_split(50.0, 10.0)
    assert s["platform_amount"] == 5.0
    assert s["walker_amount"] == 45.0


# --------------------------------------------------------------------------
# R1 — get_commission_percent: tenant SEM config deriva do plano (12/8/5),
# nunca cai no fallback legado de 20%.
# --------------------------------------------------------------------------

def test_get_commission_percent_falls_back_to_plan_default():
    db = _db()
    _tenant(db, "t-st", "starter")
    _tenant(db, "t-bz", "business")
    _tenant(db, "t-ent", "enterprise")
    # Sem TenantPaymentConfig criada → deriva do plano do tenant, não 20%.
    assert get_commission_percent(db, "t-st") == 12.0
    assert get_commission_percent(db, "t-bz") == 8.0
    assert get_commission_percent(db, "t-ent") == 5.0


def test_get_commission_percent_unknown_plan_falls_back_to_10():
    db = _db()
    _tenant(db, "t-x", "plano_inexistente")
    assert get_commission_percent(db, "t-x") == 10.0


def test_get_commission_percent_no_tenant_falls_back_to_10():
    db = _db()
    # tenant_id None ou inexistente → fallback de plano desconhecido (10), nunca 20.
    assert get_commission_percent(db, None) == 10.0
    assert get_commission_percent(db, "nao-existe") == 10.0


def test_get_commission_percent_respects_existing_config_over_plan():
    db = _db()
    _tenant(db, "t-st", "starter")  # default de plano seria 12%
    get_or_create_payment_config(db, "t-st")
    update_payment_config(db, "t-st", commission_percent=0.0)  # Fundador/sócio 0%
    # Config existente (custom 0%) prevalece sobre o default do plano.
    assert get_commission_percent(db, "t-st") == 0.0


def test_get_commission_percent_never_returns_legacy_20():
    db = _db()
    _tenant(db, "t-st", "starter")
    for tid in ("t-st", None, "nao-existe"):
        assert get_commission_percent(db, tid) != 20.0
