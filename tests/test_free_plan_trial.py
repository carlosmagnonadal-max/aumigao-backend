"""Reverse trial do plano free (duração configurável via FREE_PLAN_TRIAL_DAYS, default 7d).

- Tenant criado com plan=free ganha trial_ends_at = criação + N dias (via rota).
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
    compute_trial_ends_at,
    free_plan_trial_days,
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


# ── criação: trial de 7 dias (default) ──────────────────────────────────────

def test_compute_trial_ends_at_default_7_days(monkeypatch):
    monkeypatch.delenv("FREE_PLAN_TRIAL_DAYS", raising=False)
    base = datetime(2026, 7, 2, 12, 0)
    assert compute_trial_ends_at(base) == base + timedelta(days=7)
    assert free_plan_trial_days() == 7


def test_free_plan_trial_days_env_override(monkeypatch):
    """Duração é lida da env no momento do uso — testável sem patch de módulo."""
    monkeypatch.setenv("FREE_PLAN_TRIAL_DAYS", "14")
    assert free_plan_trial_days() == 14
    base = datetime(2026, 7, 2, 12, 0)
    assert compute_trial_ends_at(base) == base + timedelta(days=14)


def test_free_plan_trial_days_invalid_env_falls_back_to_default(monkeypatch):
    """Valor inválido na env retorna o default (7), não zera o trial por engano."""
    monkeypatch.setenv("FREE_PLAN_TRIAL_DAYS", "nope")
    assert free_plan_trial_days() == 7
    monkeypatch.setenv("FREE_PLAN_TRIAL_DAYS", "0")
    assert free_plan_trial_days() == 7
    monkeypatch.setenv("FREE_PLAN_TRIAL_DAYS", "-5")
    assert free_plan_trial_days() == 7


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
    assert timedelta(days=6) < delta <= timedelta(days=7)
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


# ── downgrade cancela assinaturas recorrentes ativas (dinheiro) ─────────────
#
# Princípio: bloqueia novo, mantém o que já foi pago; nenhuma cobrança sem
# contrapartida. No downgrade do trial, as TutorSubscription ativas do tenant free
# são canceladas (Asaas + local) para não virarem "zumbis" cobrando sem entregar.
# Créditos restantes NÃO são tocados.

from app.models.recurring_plan import (  # noqa: E402
    CANCEL_REASON_PLAN_DOWNGRADE,
    SUBSCRIPTION_ACTIVE,
    SUBSCRIPTION_CANCELLED,
    SUBSCRIPTION_OVERDUE,
    RecurringPlan,
    TutorSubscription,
)


def _seed_sub(db, tenant_id, *, sub_id, tutor_id, status=SUBSCRIPTION_ACTIVE,
              asaas_id="asaas-x", credits=5, plan_id="plan-1", reason=None,
              period_end=None):
    if db.get(RecurringPlan, plan_id) is None:
        db.add(RecurringPlan(id=plan_id, tenant_id=tenant_id, name="8 passeios/mês",
                             price=99.0, walks_per_cycle=8, interval="monthly", active=True))
    sub = TutorSubscription(
        id=sub_id, tenant_id=tenant_id, plan_id=plan_id, tutor_id=tutor_id,
        status=status, price=99.0, walks_per_cycle=8, credits_remaining=credits,
        credits_granted=True, asaas_subscription_id=asaas_id, cancel_reason=reason,
        current_period_end=period_end,
    )
    db.add(sub)
    db.commit()
    return sub


def _patch_asaas_cancel(monkeypatch, calls, *, fail_ids=frozenset()):
    """Substitui cancel_asaas_subscription (async) por um mock que registra as chamadas.

    Ao invés de rede real, guarda os ids cancelados em `calls`; se o id estiver em
    `fail_ids`, levanta para simular falha de gateway (resiliência por item).
    """
    import app.services.asaas_subscription_service as ass

    async def _fake_cancel(asaas_subscription_id):
        calls.append(asaas_subscription_id)
        if asaas_subscription_id in fail_ids:
            raise RuntimeError(f"asaas down for {asaas_subscription_id}")

    monkeypatch.setattr(ass, "cancel_asaas_subscription", _fake_cancel)


def test_downgrade_cancels_active_subscriptions(monkeypatch):
    db = _db()
    t = _expired_tenant_with_admin(db)
    _seed_sub(db, "t-down", sub_id="s1", tutor_id="tut-1", asaas_id="asaas-1", credits=5)
    _seed_sub(db, "t-down", sub_id="s2", tutor_id="tut-2", asaas_id="asaas-2",
              status=SUBSCRIPTION_OVERDUE, credits=3)
    calls = []
    _patch_asaas_cancel(monkeypatch, calls)

    assert maybe_downgrade_expired_trial(db, t) is True
    db.commit()

    # Ambas canceladas no Asaas e localmente (inclui a que estava OVERDUE).
    assert set(calls) == {"asaas-1", "asaas-2"}
    s1 = db.get(TutorSubscription, "s1")
    s2 = db.get(TutorSubscription, "s2")
    assert s1.status == SUBSCRIPTION_CANCELLED and s1.cancelled_at is not None
    assert s2.status == SUBSCRIPTION_CANCELLED and s2.cancelled_at is not None
    # Motivo carimbado (Opção B): é o que mantém os créditos consumíveis e
    # protege o saldo do sweep de breakage.
    assert s1.cancel_reason == CANCEL_REASON_PLAN_DOWNGRADE
    assert s2.cancel_reason == CANCEL_REASON_PLAN_DOWNGRADE
    # Créditos restantes NÃO tocados — o tutor esgota o que já pagou.
    assert s1.credits_remaining == 5
    assert s2.credits_remaining == 3
    # Tutor notificado (mecanismo in-app existente).
    tut_notes = db.query(Notification).filter(Notification.user_id == "tut-1").all()
    assert len(tut_notes) == 1
    assert "créditos" in tut_notes[0].message or "encerrada" in tut_notes[0].message.lower()


def test_downgrade_subscription_cancel_is_idempotent(monkeypatch):
    db = _db()
    t = _expired_tenant_with_admin(db)
    _seed_sub(db, "t-down", sub_id="s1", tutor_id="tut-1", asaas_id="asaas-1")
    calls = []
    _patch_asaas_cancel(monkeypatch, calls)

    assert maybe_downgrade_expired_trial(db, t) is True
    db.commit()
    # 2ª chamada: já carimbado + assinatura já cancelada → nada acontece de novo.
    assert maybe_downgrade_expired_trial(db, t) is False
    db.commit()
    assert calls == ["asaas-1"]  # cancelamento não duplicado
    assert db.get(TutorSubscription, "s1").status == SUBSCRIPTION_CANCELLED


def test_downgrade_one_asaas_failure_does_not_block_others(monkeypatch):
    db = _db()
    t = _expired_tenant_with_admin(db)
    _seed_sub(db, "t-down", sub_id="s1", tutor_id="tut-1", asaas_id="asaas-1")
    _seed_sub(db, "t-down", sub_id="s2", tutor_id="tut-2", asaas_id="asaas-2")
    calls = []
    _patch_asaas_cancel(monkeypatch, calls, fail_ids={"asaas-1"})

    # Falha no Asaas de s1 não aborta o downgrade nem o cancelamento de s2.
    assert maybe_downgrade_expired_trial(db, t) is True
    db.commit()
    assert set(calls) == {"asaas-1", "asaas-2"}
    # s1 falhou no gateway → permanece ativa (não marca local sem sucesso remoto);
    # s2 é cancelada normalmente.
    assert db.get(TutorSubscription, "s1").status == SUBSCRIPTION_ACTIVE
    assert db.get(TutorSubscription, "s2").status == SUBSCRIPTION_CANCELLED
    # O carimbo do downgrade acontece de qualquer forma.
    assert t.trial_downgraded_at is not None


def test_downgrade_without_subscriptions_still_stamps(monkeypatch):
    db = _db()
    t = _expired_tenant_with_admin(db)
    calls = []
    _patch_asaas_cancel(monkeypatch, calls)
    assert maybe_downgrade_expired_trial(db, t) is True
    db.commit()
    assert calls == []
    assert t.trial_downgraded_at is not None


# ── Opção B: créditos de CANCELLED-por-downgrade continuam consumíveis ───────
#
# Decisão do Carlos: "créditos já pagos permanecem usáveis até esgotar".
# consume_credit_if_available honra CANCELLED apenas com cancel_reason=
# 'plan_downgrade'; cancelada MANUAL (reason NULL) segue forfeit (breakage);
# renovação nunca reabastece cancelada; sweep de breakage pula as preservadas.

from app.services.recurring_plan_service import (  # noqa: E402
    cancel_subscription,
    consume_credit_if_available,
    refund_credit_for_walk,
    reset_credits_if_renewal,
)


def test_consume_credit_from_downgrade_cancelled_subscription():
    db = _db()
    t = _tenant(db, "t-b1", "free")
    _seed_sub(db, "t-b1", sub_id="sb1", tutor_id="tut-1",
              status=SUBSCRIPTION_CANCELLED, reason=CANCEL_REASON_PLAN_DOWNGRADE,
              credits=2)
    sub = consume_credit_if_available(db, t, "tut-1")
    db.commit()
    assert sub is not None and sub.id == "sb1"
    assert sub.credits_remaining == 1
    # Esgota e nega o próximo.
    assert consume_credit_if_available(db, t, "tut-1") is not None
    db.commit()
    assert consume_credit_if_available(db, t, "tut-1") is None


def test_consume_denied_for_downgrade_cancelled_without_credits():
    db = _db()
    t = _tenant(db, "t-b2", "free")
    _seed_sub(db, "t-b2", sub_id="sb2", tutor_id="tut-1",
              status=SUBSCRIPTION_CANCELLED, reason=CANCEL_REASON_PLAN_DOWNGRADE,
              credits=0)
    assert consume_credit_if_available(db, t, "tut-1") is None


def test_consume_denied_for_manual_cancelled_even_with_credits():
    """Cancelada MANUAL (reason NULL) NÃO consome — mesmo que o breakage tenha
    falhado em zerar (flag off/erro), o crédito segue não-consumível (forfeit)."""
    db = _db()
    t = _tenant(db, "t-b3", "free")
    _seed_sub(db, "t-b3", sub_id="sb3", tutor_id="tut-1",
              status=SUBSCRIPTION_CANCELLED, reason=None, credits=4)
    assert consume_credit_if_available(db, t, "tut-1") is None
    assert db.get(TutorSubscription, "sb3").credits_remaining == 4


def test_manual_cancel_breakage_zeroes_credits(monkeypatch):
    """Verificação 2: cancelamento MANUAL reconhece breakage e ZERA o saldo —
    comportamento inalterado; a assinatura fica sem crédito consumível."""
    monkeypatch.delenv("CREDIT_LEDGER_ENABLED", raising=False)  # default ON
    db = _db()
    t = _tenant(db, "t-b4", "pro")
    _seed_sub(db, "t-b4", sub_id="sb4", tutor_id="tut-1", credits=6)
    cancel_subscription(db, "t-b4", "tut-1")
    s = db.get(TutorSubscription, "sb4")
    assert s.status == SUBSCRIPTION_CANCELLED
    assert s.cancel_reason is None          # manual: sem motivo de downgrade
    assert s.credits_remaining == 0         # breakage zerou
    assert consume_credit_if_available(db, t, "tut-1") is None


def test_renewal_never_refills_cancelled():
    """Requisito 4: reset_credits_if_renewal barra CANCELLED (qualquer motivo) —
    webhook zumbi de renovação não reabastece crédito de assinatura cancelada."""
    db = _db()
    _tenant(db, "t-b5", "free")
    expired_period = datetime.utcnow() - timedelta(days=1)
    down = _seed_sub(db, "t-b5", sub_id="sb5", tutor_id="tut-1",
                     status=SUBSCRIPTION_CANCELLED, reason=CANCEL_REASON_PLAN_DOWNGRADE,
                     credits=1, period_end=expired_period)
    manual = _seed_sub(db, "t-b5", sub_id="sb6", tutor_id="tut-2",
                       status=SUBSCRIPTION_CANCELLED, reason=None,
                       credits=0, period_end=expired_period)
    assert reset_credits_if_renewal(db, down) is False
    assert reset_credits_if_renewal(db, manual) is False
    db.commit()
    assert db.get(TutorSubscription, "sb5").credits_remaining == 1  # não reabasteceu
    assert db.get(TutorSubscription, "sb6").credits_remaining == 0


def test_breakage_sweep_skips_downgrade_cancelled_but_zeroes_manual(monkeypatch):
    """O cron diário de breakage NÃO pode destruir os créditos preservados do
    downgrade; canceladas manuais que escaparam do breakage continuam varridas."""
    monkeypatch.delenv("CREDIT_LEDGER_ENABLED", raising=False)  # default ON
    from app.services.credit_expiry_service import sweep_expired_credits

    db = _db()
    _tenant(db, "t-b6", "free")
    _seed_sub(db, "t-b6", sub_id="sb7", tutor_id="tut-1",
              status=SUBSCRIPTION_CANCELLED, reason=CANCEL_REASON_PLAN_DOWNGRADE,
              credits=5)
    _seed_sub(db, "t-b6", sub_id="sb8", tutor_id="tut-2",
              status=SUBSCRIPTION_CANCELLED, reason=None, credits=3)
    result = sweep_expired_credits(db)
    db.commit()
    assert db.get(TutorSubscription, "sb7").credits_remaining == 5  # preservada
    assert db.get(TutorSubscription, "sb8").credits_remaining == 0  # manual varrida
    assert result["recognized"] == 1


def test_refund_credit_returns_to_downgrade_cancelled_not_manual():
    """Requisito 5: passeio cancelado devolve o crédito à CANCELLED-por-downgrade
    (o tutor recupera crédito que pagou e ainda pode usar); manual não recebe."""
    from app.models.walk import Walk

    db = _db()
    _tenant(db, "t-b7", "free")
    down = _seed_sub(db, "t-b7", sub_id="sb9", tutor_id="tut-1",
                     status=SUBSCRIPTION_CANCELLED, reason=CANCEL_REASON_PLAN_DOWNGRADE,
                     credits=1)
    manual = _seed_sub(db, "t-b7", sub_id="sb10", tutor_id="tut-2",
                       status=SUBSCRIPTION_CANCELLED, reason=None, credits=0)
    w1 = Walk(id="w-b7-1", tutor_id="tut-1", tenant_id="t-b7", pet_id="pet-x",
              scheduled_date="2026-07-01", duration_minutes=30, price=50.0,
              status="Cancelado", subscription_id="sb9", credit_refunded=False,
              created_at=datetime.utcnow())
    w2 = Walk(id="w-b7-2", tutor_id="tut-2", tenant_id="t-b7", pet_id="pet-x",
              scheduled_date="2026-07-01", duration_minutes=30, price=50.0,
              status="Cancelado", subscription_id="sb10", credit_refunded=False,
              created_at=datetime.utcnow())
    db.add_all([w1, w2])
    db.commit()

    assert refund_credit_for_walk(db, w1) is True
    assert refund_credit_for_walk(db, w2) is False
    db.commit()
    assert db.get(TutorSubscription, "sb9").credits_remaining == 2
    assert db.get(TutorSubscription, "sb10").credits_remaining == 0


def test_consume_prefers_preserved_credits_over_new_active():
    """Tutor com cancelada-por-downgrade COM saldo + ativa nova (tenant voltou ao
    Pro): consome primeiro os créditos preservados (não renovam) e debita UMA
    única assinatura por chamada (sem decremento múltiplo)."""
    db = _db()
    t = _tenant(db, "t-b8", "pro")
    _seed_sub(db, "t-b8", sub_id="sb11", tutor_id="tut-1",
              status=SUBSCRIPTION_CANCELLED, reason=CANCEL_REASON_PLAN_DOWNGRADE,
              credits=1)
    _seed_sub(db, "t-b8", sub_id="sb12", tutor_id="tut-1",
              status=SUBSCRIPTION_ACTIVE, credits=8)
    first = consume_credit_if_available(db, t, "tut-1")
    db.commit()
    assert first is not None and first.id == "sb11"  # preservada primeiro
    assert db.get(TutorSubscription, "sb11").credits_remaining == 0
    assert db.get(TutorSubscription, "sb12").credits_remaining == 8  # intocada
    second = consume_credit_if_available(db, t, "tut-1")
    db.commit()
    assert second is not None and second.id == "sb12"  # depois a ativa
    assert db.get(TutorSubscription, "sb12").credits_remaining == 7


def test_downgrade_then_consume_end_to_end(monkeypatch):
    """Fluxo completo: downgrade cancela a assinatura e o tutor segue consumindo
    os créditos que pagou até esgotar."""
    db = _db()
    t = _expired_tenant_with_admin(db)
    _seed_sub(db, "t-down", sub_id="se2e", tutor_id="tut-1", credits=2)
    calls = []
    _patch_asaas_cancel(monkeypatch, calls)
    assert maybe_downgrade_expired_trial(db, t) is True
    db.commit()
    assert db.get(TutorSubscription, "se2e").status == SUBSCRIPTION_CANCELLED
    # Consome os 2 créditos pagos; o 3º nega.
    assert consume_credit_if_available(db, t, "tut-1") is not None
    db.commit()
    assert consume_credit_if_available(db, t, "tut-1") is not None
    db.commit()
    assert consume_credit_if_available(db, t, "tut-1") is None
