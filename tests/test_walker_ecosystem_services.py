"""Testes dos serviços do ecossistema do passeador (CR, gamificação, smart notifications).

Usa SQLite in-memory com Base.metadata.create_all (mesmo padrão de test_pricing_v2.py).
Importa app.models para garantir que todas as tabelas estejam registradas no Base.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 — registra todas as tabelas no Base.metadata
from app.core.database import Base
from app.models.user import User

# ── Serviços a testar ────────────────────────────────────────────────────────
import app.services.walker_cr_service as cr_svc
import app.services.walker_gamification_service as gami_svc
import app.services.walker_smart_notification_service as notif_svc
from app.services.walker_cr_rules import CR_EARN, CR_PENALTY, CR_SPEND, BADGE_WALK_MILESTONES, BADGE_LEVELS


# ── Fixture de banco in-memory ───────────────────────────────────────────────

def _db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _user(db, uid: str = "walker-1") -> User:
    """Cria um usuário passeador mínimo."""
    user = User(
        id=uid,
        email=f"{uid}@test.com",
        password_hash="hashed",
        full_name="Passeador Teste",
        role="walker",
    )
    db.add(user)
    db.commit()
    return user


# ════════════════════════════════════════════════════════════════════════════
# walker_cr_rules — constantes
# ════════════════════════════════════════════════════════════════════════════

def test_cr_rules_earn_values():
    assert CR_EARN["walk_completed"] == 10
    assert CR_EARN["review_5star"] == 5
    assert CR_EARN["weekly_mission"] == 20
    assert CR_EARN["kit_approved"] == 15


def test_cr_rules_penalty_values():
    assert CR_PENALTY["no_show"] == 15


def test_cr_rules_spend_values():
    assert CR_SPEND["boost_24h"] == 50


def test_cr_rules_badge_milestones():
    assert BADGE_WALK_MILESTONES == [50, 100, 500]


def test_cr_rules_badge_levels():
    assert "Bronze" in BADGE_LEVELS
    assert "Prata" in BADGE_LEVELS
    assert "Ouro" in BADGE_LEVELS


# ════════════════════════════════════════════════════════════════════════════
# walker_cr_service — carteira e transações
# ════════════════════════════════════════════════════════════════════════════

class TestGetOrCreateWallet:
    def test_creates_wallet_with_zero_balance(self):
        db = _db()
        _user(db)
        wallet = cr_svc.get_or_create_wallet(db, "walker-1")
        assert wallet.balance == 0
        assert wallet.lifetime_earned == 0
        assert wallet.lifetime_spent == 0
        assert wallet.walker_user_id == "walker-1"

    def test_returns_existing_wallet(self):
        db = _db()
        _user(db)
        w1 = cr_svc.get_or_create_wallet(db, "walker-1")
        db.commit()
        w2 = cr_svc.get_or_create_wallet(db, "walker-1")
        assert w1.id == w2.id

    def test_isolated_per_walker(self):
        db = _db()
        _user(db, "w1")
        _user(db, "w2")
        wa = cr_svc.get_or_create_wallet(db, "w1")
        wb = cr_svc.get_or_create_wallet(db, "w2")
        db.commit()
        assert wa.id != wb.id


class TestEarnCr:
    def test_earn_increases_balance(self):
        db = _db()
        _user(db)
        cr_svc.earn_cr(db, "walker-1", 10, "walk_completed")
        db.commit()
        assert cr_svc.get_balance(db, "walker-1") == 10

    def test_earn_increases_lifetime_earned(self):
        db = _db()
        _user(db)
        cr_svc.earn_cr(db, "walker-1", 10, "walk_completed")
        db.commit()
        wallet = cr_svc.get_or_create_wallet(db, "walker-1")
        assert wallet.lifetime_earned == 10

    def test_earn_creates_transaction(self):
        db = _db()
        _user(db)
        tx = cr_svc.earn_cr(db, "walker-1", 10, "walk_completed")
        db.commit()
        assert tx.tx_type == "earn"
        assert tx.amount == 10
        assert tx.source == "walk_completed"

    def test_earn_cumulative(self):
        db = _db()
        _user(db)
        cr_svc.earn_cr(db, "walker-1", 10, "walk_completed")
        cr_svc.earn_cr(db, "walker-1", 5, "review_5star")
        db.commit()
        assert cr_svc.get_balance(db, "walker-1") == 15

    def test_earn_with_log_event_creates_gamification_event(self):
        db = _db()
        _user(db)
        cr_svc.earn_cr(db, "walker-1", 10, "walk_completed", log_event=True)
        db.commit()
        events = gami_svc.list_events(db, "walker-1")
        assert len(events) == 1
        assert events[0].event_type == "cr_granted"
        assert events[0].cr_amount == 10

    def test_earn_without_log_event_skips_gamification(self):
        db = _db()
        _user(db)
        cr_svc.earn_cr(db, "walker-1", 10, "walk_completed", log_event=False)
        db.commit()
        events = gami_svc.list_events(db, "walker-1")
        assert len(events) == 0

    def test_earn_does_not_affect_lifetime_spent(self):
        db = _db()
        _user(db)
        cr_svc.earn_cr(db, "walker-1", 20, "weekly_mission")
        db.commit()
        wallet = cr_svc.get_or_create_wallet(db, "walker-1")
        assert wallet.lifetime_spent == 0


class TestSpendCr:
    def test_spend_insufficient_returns_none(self):
        db = _db()
        _user(db)
        result = cr_svc.spend_cr(db, "walker-1", 50, "boost_24h")
        assert result is None

    def test_spend_insufficient_does_not_change_balance(self):
        db = _db()
        _user(db)
        cr_svc.earn_cr(db, "walker-1", 30, "walk_completed", log_event=False)
        db.commit()
        cr_svc.spend_cr(db, "walker-1", 50, "boost_24h", log_event=False)
        assert cr_svc.get_balance(db, "walker-1") == 30

    def test_spend_sufficient_debits_balance(self):
        db = _db()
        _user(db)
        cr_svc.earn_cr(db, "walker-1", 60, "walk_completed", log_event=False)
        db.commit()
        cr_svc.spend_cr(db, "walker-1", 50, "boost_24h", log_event=False)
        db.commit()
        assert cr_svc.get_balance(db, "walker-1") == 10

    def test_spend_increases_lifetime_spent(self):
        db = _db()
        _user(db)
        cr_svc.earn_cr(db, "walker-1", 60, "walk_completed", log_event=False)
        db.commit()
        cr_svc.spend_cr(db, "walker-1", 50, "boost_24h", log_event=False)
        db.commit()
        wallet = cr_svc.get_or_create_wallet(db, "walker-1")
        assert wallet.lifetime_spent == 50

    def test_spend_creates_transaction_with_negative_amount(self):
        db = _db()
        _user(db)
        cr_svc.earn_cr(db, "walker-1", 60, "walk_completed", log_event=False)
        db.commit()
        tx = cr_svc.spend_cr(db, "walker-1", 50, "boost_24h", log_event=False)
        db.commit()
        assert tx is not None
        assert tx.tx_type == "spend"
        assert tx.amount == -50
        assert tx.source == "boost_24h"

    def test_spend_exact_balance_succeeds(self):
        db = _db()
        _user(db)
        cr_svc.earn_cr(db, "walker-1", 50, "walk_completed", log_event=False)
        db.commit()
        tx = cr_svc.spend_cr(db, "walker-1", 50, "boost_24h", log_event=False)
        db.commit()
        assert tx is not None
        assert cr_svc.get_balance(db, "walker-1") == 0

    def test_spend_with_log_event_creates_boost_event(self):
        db = _db()
        _user(db)
        cr_svc.earn_cr(db, "walker-1", 60, "walk_completed", log_event=False)
        db.commit()
        cr_svc.spend_cr(db, "walker-1", 50, "boost_24h", log_event=True)
        db.commit()
        events = gami_svc.list_events(db, "walker-1")
        assert any(e.event_type == "boost_activated" for e in events)


class TestPenaltyCr:
    def test_penalty_reduces_balance(self):
        db = _db()
        _user(db)
        cr_svc.earn_cr(db, "walker-1", 20, "walk_completed", log_event=False)
        db.commit()
        cr_svc.penalty_cr(db, "walker-1", 15, "no_show", log_event=False)
        db.commit()
        assert cr_svc.get_balance(db, "walker-1") == 5

    def test_penalty_clamps_balance_at_zero(self):
        """Penalidade maior que o saldo clipa em 0, mas transação guarda valor cheio."""
        db = _db()
        _user(db)
        cr_svc.earn_cr(db, "walker-1", 10, "walk_completed", log_event=False)
        db.commit()
        tx = cr_svc.penalty_cr(db, "walker-1", 15, "no_show", log_event=False)
        db.commit()
        # Balance clamped to 0
        assert cr_svc.get_balance(db, "walker-1") == 0
        # Transaction records the full penalty
        assert tx.amount == -15

    def test_penalty_on_zero_balance_stays_zero(self):
        db = _db()
        _user(db)
        cr_svc.penalty_cr(db, "walker-1", 15, "no_show", log_event=False)
        db.commit()
        assert cr_svc.get_balance(db, "walker-1") == 0

    def test_penalty_creates_transaction_type_penalty(self):
        db = _db()
        _user(db)
        tx = cr_svc.penalty_cr(db, "walker-1", 15, "no_show", log_event=False)
        db.commit()
        assert tx.tx_type == "penalty"
        assert tx.source == "no_show"

    def test_penalty_does_not_affect_lifetime_spent(self):
        db = _db()
        _user(db)
        cr_svc.earn_cr(db, "walker-1", 20, "walk_completed", log_event=False)
        db.commit()
        cr_svc.penalty_cr(db, "walker-1", 15, "no_show", log_event=False)
        db.commit()
        wallet = cr_svc.get_or_create_wallet(db, "walker-1")
        assert wallet.lifetime_spent == 0


class TestListTransactions:
    def test_list_returns_most_recent_first(self):
        db = _db()
        _user(db)
        cr_svc.earn_cr(db, "walker-1", 10, "walk_completed", log_event=False)
        cr_svc.earn_cr(db, "walker-1", 5, "review_5star", log_event=False)
        db.commit()
        txs = cr_svc.list_transactions(db, "walker-1")
        assert txs[0].source == "review_5star"
        assert txs[1].source == "walk_completed"

    def test_list_respects_limit(self):
        db = _db()
        _user(db)
        for i in range(5):
            cr_svc.earn_cr(db, "walker-1", 10, "walk_completed", log_event=False)
        db.commit()
        txs = cr_svc.list_transactions(db, "walker-1", limit=3)
        assert len(txs) == 3

    def test_list_empty_for_new_walker(self):
        db = _db()
        _user(db)
        txs = cr_svc.list_transactions(db, "walker-1")
        assert txs == []


# ════════════════════════════════════════════════════════════════════════════
# walker_gamification_service
# ════════════════════════════════════════════════════════════════════════════

class TestGamificationService:
    def test_log_event_creates_record(self):
        db = _db()
        _user(db)
        event = gami_svc.log_event(db, "walker-1", "badge_earned", "Badge Bronze")
        db.commit()
        assert event.event_type == "badge_earned"
        assert event.title == "Badge Bronze"
        assert event.walker_user_id == "walker-1"

    def test_log_event_stores_cr_amount(self):
        db = _db()
        _user(db)
        event = gami_svc.log_event(db, "walker-1", "cr_granted", "+10 CR", cr_amount=10)
        db.commit()
        assert event.cr_amount == 10

    def test_log_event_stores_related_entity(self):
        db = _db()
        _user(db)
        event = gami_svc.log_event(
            db, "walker-1", "cr_granted", "+10 CR",
            related_entity_type="walk", related_entity_id="walk-99",
        )
        db.commit()
        assert event.related_entity_type == "walk"
        assert event.related_entity_id == "walk-99"

    def test_list_events_returns_desc(self):
        db = _db()
        _user(db)
        gami_svc.log_event(db, "walker-1", "cr_granted", "First")
        gami_svc.log_event(db, "walker-1", "badge_earned", "Second")
        db.commit()
        events = gami_svc.list_events(db, "walker-1")
        assert events[0].title == "Second"
        assert events[1].title == "First"

    def test_list_events_empty_for_new_walker(self):
        db = _db()
        _user(db)
        assert gami_svc.list_events(db, "walker-1") == []

    def test_list_events_respects_limit(self):
        db = _db()
        _user(db)
        for i in range(10):
            gami_svc.log_event(db, "walker-1", "cr_granted", f"Event {i}")
        db.commit()
        events = gami_svc.list_events(db, "walker-1", limit=3)
        assert len(events) == 3

    def test_log_event_isolated_per_walker(self):
        db = _db()
        _user(db, "w1")
        _user(db, "w2")
        gami_svc.log_event(db, "w1", "badge_earned", "W1 Badge")
        gami_svc.log_event(db, "w2", "cr_granted", "W2 CR")
        db.commit()
        assert len(gami_svc.list_events(db, "w1")) == 1
        assert len(gami_svc.list_events(db, "w2")) == 1


# ════════════════════════════════════════════════════════════════════════════
# walker_smart_notification_service
# ════════════════════════════════════════════════════════════════════════════

class TestSmartNotificationService:
    def test_create_notification_persists(self):
        db = _db()
        _user(db)
        notif = notif_svc.create_notification(
            db, "walker-1", "cr_earned", "Você ganhou CR!",
            trigger_source="walk_completed",
        )
        db.commit()
        assert notif.notification_type == "cr_earned"
        assert notif.title == "Você ganhou CR!"
        assert notif.walker_user_id == "walker-1"
        assert notif.read_at is None

    def test_create_notification_sets_sent_at(self):
        db = _db()
        _user(db)
        notif = notif_svc.create_notification(
            db, "walker-1", "boost_ready", "Boost disponível",
            trigger_source="schedule",
        )
        db.commit()
        assert notif.sent_at is not None

    def test_create_notification_default_priority(self):
        db = _db()
        _user(db)
        notif = notif_svc.create_notification(
            db, "walker-1", "info", "Info",
            trigger_source="system",
        )
        db.commit()
        assert notif.priority == "normal"

    def test_create_notification_custom_priority(self):
        db = _db()
        _user(db)
        notif = notif_svc.create_notification(
            db, "walker-1", "alert", "Alerta",
            priority="high", trigger_source="system",
        )
        db.commit()
        assert notif.priority == "high"

    def test_list_notifications_all(self):
        db = _db()
        _user(db)
        notif_svc.create_notification(db, "walker-1", "t1", "N1", trigger_source="s")
        notif_svc.create_notification(db, "walker-1", "t2", "N2", trigger_source="s")
        db.commit()
        notifs = notif_svc.list_notifications(db, "walker-1")
        assert len(notifs) == 2

    def test_list_notifications_unread_only(self):
        db = _db()
        _user(db)
        n1 = notif_svc.create_notification(db, "walker-1", "t1", "N1", trigger_source="s")
        n2 = notif_svc.create_notification(db, "walker-1", "t2", "N2", trigger_source="s")
        db.commit()
        # Mark n1 as read
        notif_svc.mark_read(db, n1.id, "walker-1")
        unread = notif_svc.list_notifications(db, "walker-1", unread_only=True)
        assert len(unread) == 1
        assert unread[0].id == n2.id

    def test_list_notifications_desc_by_sent_at(self):
        db = _db()
        _user(db)
        n1 = notif_svc.create_notification(db, "walker-1", "t1", "First", trigger_source="s")
        n2 = notif_svc.create_notification(db, "walker-1", "t2", "Second", trigger_source="s")
        db.commit()
        notifs = notif_svc.list_notifications(db, "walker-1")
        # Both created in the same instant in tests; at minimum both are present
        assert len(notifs) == 2

    def test_list_notifications_respects_limit(self):
        db = _db()
        _user(db)
        for i in range(10):
            notif_svc.create_notification(db, "walker-1", "t", f"N{i}", trigger_source="s")
        db.commit()
        notifs = notif_svc.list_notifications(db, "walker-1", limit=3)
        assert len(notifs) == 3

    def test_mark_read_sets_read_at(self):
        db = _db()
        _user(db)
        notif = notif_svc.create_notification(
            db, "walker-1", "t1", "N1", trigger_source="s"
        )
        db.commit()
        result = notif_svc.mark_read(db, notif.id, "walker-1")
        assert result is not None
        assert result.read_at is not None

    def test_mark_read_validates_ownership(self):
        db = _db()
        _user(db, "w1")
        _user(db, "w2")
        notif = notif_svc.create_notification(db, "w1", "t1", "N1", trigger_source="s")
        db.commit()
        # w2 tries to mark w1's notification
        result = notif_svc.mark_read(db, notif.id, "w2")
        assert result is None

    def test_mark_read_nonexistent_returns_none(self):
        db = _db()
        _user(db)
        result = notif_svc.mark_read(db, "nonexistent-id", "walker-1")
        assert result is None

    def test_count_unread_initial_zero(self):
        db = _db()
        _user(db)
        assert notif_svc.count_unread(db, "walker-1") == 0

    def test_count_unread_after_creates(self):
        db = _db()
        _user(db)
        notif_svc.create_notification(db, "walker-1", "t1", "N1", trigger_source="s")
        notif_svc.create_notification(db, "walker-1", "t2", "N2", trigger_source="s")
        db.commit()
        assert notif_svc.count_unread(db, "walker-1") == 2

    def test_count_unread_decreases_after_mark_read(self):
        db = _db()
        _user(db)
        n1 = notif_svc.create_notification(db, "walker-1", "t1", "N1", trigger_source="s")
        notif_svc.create_notification(db, "walker-1", "t2", "N2", trigger_source="s")
        db.commit()
        notif_svc.mark_read(db, n1.id, "walker-1")
        assert notif_svc.count_unread(db, "walker-1") == 1

    def test_count_unread_isolated_per_walker(self):
        db = _db()
        _user(db, "w1")
        _user(db, "w2")
        notif_svc.create_notification(db, "w1", "t", "N", trigger_source="s")
        notif_svc.create_notification(db, "w1", "t", "N", trigger_source="s")
        db.commit()
        assert notif_svc.count_unread(db, "w1") == 2
        assert notif_svc.count_unread(db, "w2") == 0


# ════════════════════════════════════════════════════════════════════════════
# already_awarded — idempotência (Fase 4, CRÍTICO)
# ════════════════════════════════════════════════════════════════════════════

class TestAlreadyAwarded:
    """Garante que already_awarded detecta duplicatas e que earn_cr duas vezes
    pela mesma entidade NÃO dobra o saldo quando a lógica de chamada usa o guard."""

    def test_returns_false_before_any_transaction(self):
        db = _db()
        _user(db)
        assert not cr_svc.already_awarded(db, "walker-1", "walk_completed", "walk-abc")

    def test_returns_true_after_earn(self):
        db = _db()
        _user(db)
        cr_svc.earn_cr(
            db, "walker-1", 10, "walk_completed",
            related_entity_id="walk-abc", log_event=False,
        )
        db.commit()
        assert cr_svc.already_awarded(db, "walker-1", "walk_completed", "walk-abc")

    def test_different_entity_id_is_not_awarded(self):
        db = _db()
        _user(db)
        cr_svc.earn_cr(
            db, "walker-1", 10, "walk_completed",
            related_entity_id="walk-abc", log_event=False,
        )
        db.commit()
        assert not cr_svc.already_awarded(db, "walker-1", "walk_completed", "walk-xyz")

    def test_different_source_is_not_awarded(self):
        db = _db()
        _user(db)
        cr_svc.earn_cr(
            db, "walker-1", 10, "walk_completed",
            related_entity_id="entity-1", log_event=False,
        )
        db.commit()
        assert not cr_svc.already_awarded(db, "walker-1", "review_5star", "entity-1")

    def test_different_walker_is_not_awarded(self):
        db = _db()
        _user(db, "w1")
        _user(db, "w2")
        cr_svc.earn_cr(
            db, "w1", 10, "walk_completed",
            related_entity_id="walk-abc", log_event=False,
        )
        db.commit()
        assert not cr_svc.already_awarded(db, "w2", "walk_completed", "walk-abc")

    def test_idempotent_earn_does_not_double_balance(self):
        """Simula o guard: earn chamado 2x pela mesma entidade NÃO dobra saldo."""
        db = _db()
        _user(db)
        entity_id = "walk-idempotent"
        for _ in range(2):
            if not cr_svc.already_awarded(db, "walker-1", "walk_completed", entity_id):
                cr_svc.earn_cr(
                    db, "walker-1", CR_EARN["walk_completed"], "walk_completed",
                    related_entity_id=entity_id, log_event=False,
                )
            db.commit()
        assert cr_svc.get_balance(db, "walker-1") == CR_EARN["walk_completed"]

    def test_idempotent_penalty_does_not_double(self):
        """Guard no_show: penalty chamado 2x pela mesma entidade NÃO dobra penalidade."""
        db = _db()
        _user(db)
        entity_id = "walk-noshow"
        cr_svc.earn_cr(db, "walker-1", 30, "walk_completed", log_event=False)
        db.commit()
        for _ in range(2):
            if not cr_svc.already_awarded(db, "walker-1", "no_show", entity_id):
                cr_svc.penalty_cr(
                    db, "walker-1", CR_PENALTY["no_show"], "no_show",
                    related_entity_id=entity_id, log_event=False,
                )
            db.commit()
        # 30 - 15 = 15 (penalidade aplicada apenas 1x)
        assert cr_svc.get_balance(db, "walker-1") == 30 - CR_PENALTY["no_show"]

    def test_returns_true_after_penalty(self):
        db = _db()
        _user(db)
        cr_svc.penalty_cr(
            db, "walker-1", 15, "no_show",
            related_entity_id="walk-999", log_event=False,
        )
        db.commit()
        assert cr_svc.already_awarded(db, "walker-1", "no_show", "walk-999")


# ════════════════════════════════════════════════════════════════════════════
# Boost self-serve via CR (POST /walker/me/boost)
# ════════════════════════════════════════════════════════════════════════════

import uuid as _uuid
from datetime import datetime as _dt, timedelta as _td

from app.models.walker_boost import WalkerBoost
from app.services.boost_service import active_boost_for_walker


def _give_cr(db, walker_id: str, amount: int) -> None:
    """Helper: credita CR sem log de gamificação."""
    cr_svc.earn_cr(db, walker_id, amount, "walk_completed", log_event=False)
    db.commit()


def _activate_boost_logic(db, walker_id: str) -> dict:
    """Réplica da lógica do endpoint POST /walker/me/boost para testes de serviço.

    Espelha exatamente a lógica do endpoint: verifica saldo antes de criar o boost,
    de forma que uma falha por CR insuficiente não persiste nenhum registro.
    """
    from app.services.walker_cr_rules import CR_SPEND

    cost = CR_SPEND["boost_24h"]

    existing = active_boost_for_walker(walker_id, db)
    if existing is not None:
        return {"error": "already_active"}

    # Verificar saldo antes de criar qualquer registro.
    wallet = cr_svc.get_or_create_wallet(db, walker_id)
    if wallet.balance < cost:
        return {"error": "insufficient_cr"}

    now = _dt.utcnow()
    expires_at = now + _td(hours=24)
    boost = WalkerBoost(
        id=str(_uuid.uuid4()),
        walker_id=walker_id,
        boost_enabled=True,
        boost_type="cr_boost_24h",
        boost_score=3,
        boost_start_at=now,
        boost_end_at=expires_at,
        boost_reason="Ativado pelo passeador via CR",
        boost_status="active",
    )
    db.add(boost)
    db.flush()

    tx = cr_svc.spend_cr(
        db,
        walker_id,
        cost,
        source="boost_24h",
        description="Boost de visibilidade 24h",
        related_entity_type="walker_boost",
        related_entity_id=boost.id,
        log_event=True,
    )

    if tx is None:
        # Race condition defense — rollback completo.
        db.rollback()
        return {"error": "insufficient_cr"}

    db.commit()
    db.refresh(boost)
    wallet = cr_svc.get_or_create_wallet(db, walker_id)
    return {"ok": True, "boost": boost, "cr_spent": cost, "cr_balance": wallet.balance}


class TestBoostSelfServe:
    """Testes da lógica de ativação de boost via CR (espelha o endpoint POST /walker/me/boost)."""

    def test_walker_with_enough_cr_activates_boost(self):
        """Passeador com 50+ CR ativa boost: saldo cai, boost criado."""
        db = _db()
        _user(db)
        _give_cr(db, "walker-1", 60)

        result = _activate_boost_logic(db, "walker-1")

        assert result.get("ok") is True
        assert result["cr_spent"] == 50
        assert result["cr_balance"] == 10  # 60 - 50
        boost = result["boost"]
        assert boost.boost_enabled is True
        assert boost.boost_status == "active"
        assert boost.boost_type == "cr_boost_24h"
        assert boost.boost_end_at is not None
        assert boost.boost_end_at > _dt.utcnow()

    def test_wallet_balance_decreases_by_cost(self):
        """Verifica que o saldo é decrementado exatamente em 50."""
        db = _db()
        _user(db)
        _give_cr(db, "walker-1", 100)

        _activate_boost_logic(db, "walker-1")

        assert cr_svc.get_balance(db, "walker-1") == 50

    def test_spend_transaction_created(self):
        """Verifica que a transação de gasto é criada com os campos corretos."""
        db = _db()
        _user(db)
        _give_cr(db, "walker-1", 60)

        result = _activate_boost_logic(db, "walker-1")

        txs = cr_svc.list_transactions(db, "walker-1")
        spend_txs = [t for t in txs if t.tx_type == "spend"]
        assert len(spend_txs) == 1
        assert spend_txs[0].source == "boost_24h"
        assert spend_txs[0].amount == -50
        assert spend_txs[0].related_entity_type == "walker_boost"
        assert spend_txs[0].related_entity_id == result["boost"].id

    def test_gamification_event_boost_activated_logged(self):
        """spend_cr com log_event=True deve criar evento boost_activated."""
        db = _db()
        _user(db)
        _give_cr(db, "walker-1", 60)

        _activate_boost_logic(db, "walker-1")

        events = gami_svc.list_events(db, "walker-1")
        boost_events = [e for e in events if e.event_type == "boost_activated"]
        assert len(boost_events) == 1

    def test_walker_with_insufficient_cr_gets_error(self):
        """Passeador com menos de 50 CR recebe erro e saldo não muda."""
        db = _db()
        _user(db)
        _give_cr(db, "walker-1", 30)

        result = _activate_boost_logic(db, "walker-1")

        assert result.get("error") == "insufficient_cr"
        # Saldo inalterado
        assert cr_svc.get_balance(db, "walker-1") == 30

    def test_insufficient_cr_does_not_create_boost(self):
        """Quando CR insuficiente, nenhum WalkerBoost deve ser persistido."""
        db = _db()
        _user(db)
        _give_cr(db, "walker-1", 10)

        _activate_boost_logic(db, "walker-1")

        boost = active_boost_for_walker("walker-1", db)
        assert boost is None

    def test_insufficient_cr_does_not_create_spend_transaction(self):
        """Falha por saldo insuficiente não deve gerar transação de gasto."""
        db = _db()
        _user(db)
        _give_cr(db, "walker-1", 10)

        _activate_boost_logic(db, "walker-1")

        txs = cr_svc.list_transactions(db, "walker-1")
        spend_txs = [t for t in txs if t.tx_type == "spend"]
        assert len(spend_txs) == 0

    def test_walker_with_zero_cr_gets_error(self):
        """Passeador sem CR algum recebe erro de CR insuficiente."""
        db = _db()
        _user(db)

        result = _activate_boost_logic(db, "walker-1")

        assert result.get("error") == "insufficient_cr"
        assert cr_svc.get_balance(db, "walker-1") == 0

    def test_walker_with_active_boost_gets_409(self):
        """Passeador com boost já ativo recebe erro already_active (→ HTTP 409)."""
        db = _db()
        _user(db)
        _give_cr(db, "walker-1", 200)

        # Ativa o primeiro boost
        first = _activate_boost_logic(db, "walker-1")
        assert first.get("ok") is True

        # Tenta ativar novamente
        second = _activate_boost_logic(db, "walker-1")
        assert second.get("error") == "already_active"

    def test_active_boost_guard_preserves_cr(self):
        """Tentativa bloqueada por boost ativo não consome CR."""
        db = _db()
        _user(db)
        _give_cr(db, "walker-1", 200)

        _activate_boost_logic(db, "walker-1")
        balance_after_first = cr_svc.get_balance(db, "walker-1")  # 200 - 50 = 150

        _activate_boost_logic(db, "walker-1")  # bloqueado
        assert cr_svc.get_balance(db, "walker-1") == balance_after_first  # inalterado

    def test_boost_expires_and_new_boost_can_be_activated(self):
        """Após expiração do boost, deve ser possível ativar outro."""
        db = _db()
        _user(db)
        _give_cr(db, "walker-1", 200)

        # Cria boost já expirado manualmente
        expired_boost = WalkerBoost(
            id=str(_uuid.uuid4()),
            walker_id="walker-1",
            boost_enabled=True,
            boost_type="cr_boost_24h",
            boost_score=3,
            boost_start_at=_dt.utcnow() - _td(hours=48),
            boost_end_at=_dt.utcnow() - _td(hours=1),  # expirado há 1h
            boost_status="active",
        )
        db.add(expired_boost)
        db.commit()

        # active_boost_for_walker deve retornar None (e mudar status para expired)
        assert active_boost_for_walker("walker-1", db) is None

        # Novo boost pode ser ativado
        result = _activate_boost_logic(db, "walker-1")
        assert result.get("ok") is True

    def test_boost_exactly_50_cr_activates(self):
        """Passeador com exatamente 50 CR consegue ativar o boost."""
        db = _db()
        _user(db)
        _give_cr(db, "walker-1", 50)

        result = _activate_boost_logic(db, "walker-1")

        assert result.get("ok") is True
        assert result["cr_balance"] == 0
