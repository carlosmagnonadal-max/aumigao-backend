"""Reverse trial de 21 dias do plano free.

- Tenant criado com plan=free ganha trial_ends_at = criação + 21d (via rota).
- DINHEIRO É STATELESS: comissão em trial = Pro (10%); expirado = free (20%) —
  mesmo sem o carimbo de downgrade ter rodado. Override custom prevalece.
- maybe_downgrade_expired_trial: carimba UMA vez (idempotente), garante config
  em 20% e notifica os admins do tenant (loss aversion).
"""
from datetime import datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.core.database import Base, get_db
from app.dependencies.auth import get_current_user
from app.models.notification import Notification
from app.models.tenant import Tenant
from app.models.user import User
from app.routes import tenants as tenants_routes
from app.services.payment_split_service import (
    get_commission_percent,
    get_or_create_payment_config,
    update_payment_config,
)
from app.services.tenant_free_plan_service import (
    FREE_PLAN_TRIAL_DAYS,
    compute_trial_ends_at,
    maybe_downgrade_expired_trial,
)


def _db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _tenant(db, tid, plan, **kw) -> Tenant:
    t = Tenant(id=tid, name=tid, slug=tid, status="active", plan=plan, **kw)
    db.add(t)
    db.commit()
    return t


# ── criação: trial de 21 dias ───────────────────────────────────────────────

def test_compute_trial_ends_at_is_21_days():
    base = datetime(2026, 7, 2, 12, 0)
    assert compute_trial_ends_at(base) == base + timedelta(days=21)
    assert FREE_PLAN_TRIAL_DAYS == 21


def test_create_tenant_free_sets_trial():
    db = _db()
    db.add(User(id="sa", email="sa@x.com", password_hash="x", role="super_admin"))
    db.commit()
    app = FastAPI()
    app.include_router(tenants_routes.router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: db.get(User, "sa")
    c = TestClient(app)

    r = c.post("/admin/tenants", json={"name": "Novo Free", "slug": "novo-free", "plan": "free"})
    assert r.status_code == 200, r.text
    t = db.query(Tenant).filter(Tenant.slug == "novo-free").first()
    assert t is not None and t.plan == "free"
    assert t.trial_ends_at is not None
    delta = t.trial_ends_at - datetime.utcnow()
    assert timedelta(days=20) < delta <= timedelta(days=21)
    assert t.trial_downgraded_at is None
    # Config de pagamento nasce no default do plano free (20%).
    assert get_or_create_payment_config(db, t.id).commission_percent == 20.0


def test_create_tenant_pro_has_no_trial():
    db = _db()
    db.add(User(id="sa", email="sa@x.com", password_hash="x", role="super_admin"))
    db.commit()
    app = FastAPI()
    app.include_router(tenants_routes.router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: db.get(User, "sa")
    c = TestClient(app)
    r = c.post("/admin/tenants", json={"name": "Novo Pro", "slug": "novo-pro", "plan": "pro"})
    assert r.status_code == 200, r.text
    t = db.query(Tenant).filter(Tenant.slug == "novo-pro").first()
    assert t.trial_ends_at is None


# ── dinheiro stateless: comissão 10% no trial, 20% depois ───────────────────

def test_commission_10_during_trial_20_after():
    db = _db()
    trial = _tenant(db, "t-trial", "free", trial_ends_at=datetime.utcnow() + timedelta(days=5))
    expired = _tenant(db, "t-exp", "free", trial_ends_at=datetime.utcnow() - timedelta(days=1))
    # Config nasce com o default do free (20%) em ambos.
    assert get_or_create_payment_config(db, "t-trial").commission_percent == 20.0
    assert get_or_create_payment_config(db, "t-exp").commission_percent == 20.0
    # Trial ativo → cobra comissão do Pro (10%), SEM depender de carimbo.
    assert get_commission_percent(db, "t-trial") == 10.0
    # Expirado → volta ao free (20%) imediatamente (stateless).
    assert get_commission_percent(db, "t-exp") == 20.0


def test_commission_custom_override_wins_over_trial():
    db = _db()
    _tenant(db, "t-cust", "free", trial_ends_at=datetime.utcnow() + timedelta(days=5))
    get_or_create_payment_config(db, "t-cust")
    update_payment_config(db, "t-cust", commission_percent=0.0)  # negociado (ex.: fundador)
    assert get_commission_percent(db, "t-cust") == 0.0  # custom > trial


def test_commission_without_config_uses_effective_plan():
    db = _db()
    _tenant(db, "t-trial2", "free", trial_ends_at=datetime.utcnow() + timedelta(days=5))
    _tenant(db, "t-exp2", "free", trial_ends_at=datetime.utcnow() - timedelta(days=1))
    # Sem TenantPaymentConfig → fallback pelo plano EFETIVO.
    assert get_commission_percent(db, "t-trial2") == 10.0
    assert get_commission_percent(db, "t-exp2") == 20.0


def test_commission_pro_enterprise_unchanged_by_trial_logic():
    db = _db()
    _tenant(db, "t-pro", "pro")
    _tenant(db, "t-ent", "enterprise")
    assert get_commission_percent(db, "t-pro") == 10.0
    assert get_commission_percent(db, "t-ent") == 5.0


# ── downgrade lazy: carimbo idempotente + notificação ───────────────────────

def _expired_tenant_with_admin(db):
    t = _tenant(db, "t-down", "free", trial_ends_at=datetime.utcnow() - timedelta(days=1))
    db.add(User(id="adm-1", email="adm@x.com", password_hash="x", role="admin",
                tenant_id="t-down", is_active=True))
    db.commit()
    return t


def test_downgrade_stamps_once_and_notifies_admin():
    db = _db()
    t = _expired_tenant_with_admin(db)
    assert maybe_downgrade_expired_trial(db, t) is True
    db.commit()
    assert t.trial_downgraded_at is not None
    notes = db.query(Notification).filter(Notification.user_id == "adm-1").all()
    assert len(notes) == 1
    assert "plano Pro" in notes[0].message or "Pro" in notes[0].title
    # Idempotente: segunda chamada não carimba nem duplica notificação.
    assert maybe_downgrade_expired_trial(db, t) is False
    db.commit()
    assert db.query(Notification).filter(Notification.user_id == "adm-1").count() == 1


def test_downgrade_noop_while_trial_active_or_non_free():
    db = _db()
    active = _tenant(db, "t-act", "free", trial_ends_at=datetime.utcnow() + timedelta(days=5))
    pro = _tenant(db, "t-pro", "pro")
    no_trial = _tenant(db, "t-nt", "free")  # free sem trial (nunca teve)
    assert maybe_downgrade_expired_trial(db, active) is False
    assert maybe_downgrade_expired_trial(db, pro) is False
    assert maybe_downgrade_expired_trial(db, no_trial) is False
    assert active.trial_downgraded_at is None


def test_downgrade_resets_config_drift_but_respects_custom():
    db = _db()
    t = _expired_tenant_with_admin(db)
    cfg = get_or_create_payment_config(db, "t-down")
    cfg.commission_percent = 10.0  # drift hipotético (ficou do trial)
    db.commit()
    maybe_downgrade_expired_trial(db, t)
    db.commit()
    assert get_or_create_payment_config(db, "t-down").commission_percent == 20.0
