"""Testes focados na propagação de tenant_id — Part A da migration 0046.

Verificam que os INSERTs nas tabelas protected_chat_messages e
shared_walk_participants propagam corretamente tenant_id do pai (walk ou
shared_walk) — requisito crítico para que a WITH CHECK estrita do RLS (0046)
não bloqueie inserções legítimas quando a migration for aplicada em produção.

Rodando em SQLite (StaticPool) — RLS não é testável aqui (PostgreSQL-only),
mas a propagação de tenant_id é 100% app-layer e testável.
"""
from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 — registra todas as tabelas no Base.metadata
from app.core.database import Base, get_db
from app.dependencies.auth import get_current_user
from app.models.pet import Pet
from app.models.protected_chat_message import ProtectedChatMessage
from app.models.shared_walk import (
    SharedWalk,
    SharedWalkParticipant,
    TenantSharedWalkConfig,
)
from app.models.tenant import Tenant, TenantFeature
from app.models.tenant_payment_config import TenantPaymentConfig
from app.models.user import User
from app.models.walk import Walk
from app.routes import protected_chat
from app.services import shared_walk_service as svc
from app.services.tenant_seed_service import DEFAULT_TENANT_SLUG

# ---------------------------------------------------------------------------
# Helpers de setup
# ---------------------------------------------------------------------------

TENANT_ID = "t-0046"
TUTOR_ID = "tutor-0046"
WALKER_ID = "walker-0046"
WALK_ID = "walk-0046"
PET_ID = "pet-0046"


def _scheduled_now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat()


# ---------------------------------------------------------------------------
# Part A-1: protected_chat_messages.tenant_id propagado de walk.tenant_id
# ---------------------------------------------------------------------------

class TestProtectedChatTenantId:
    """INSERT em protected_chat_messages deve copiar tenant_id do Walk pai."""

    def _build(self, walk_tenant_id: str | None = TENANT_ID):
        engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)
        db = Session()

        db.add(Tenant(id=TENANT_ID, name="Aumigao", slug=DEFAULT_TENANT_SLUG, status="active", plan="business"))
        db.add(User(id=TUTOR_ID, email="t0046@t.com", password_hash="x", role="cliente", tenant_id=TENANT_ID))
        db.add(User(id=WALKER_ID, email="w0046@t.com", password_hash="x", role="passeador", tenant_id=TENANT_ID))
        db.add(Pet(id=PET_ID, tutor_id=TUTOR_ID, name="Bolt"))
        db.add(Walk(
            id=WALK_ID,
            tutor_id=TUTOR_ID,
            tenant_id=walk_tenant_id,
            walker_id=WALKER_ID,
            assigned_walker_id=WALKER_ID,
            pet_id=PET_ID,
            scheduled_date=_scheduled_now_iso(),
            duration_minutes=45,
            price=50.0,
            status="walker_accepted",
            operational_status="ride_in_progress",
        ))
        db.commit()

        test_app = FastAPI()
        test_app.include_router(protected_chat.router)
        test_app.dependency_overrides[get_db] = lambda: db
        test_app.dependency_overrides[get_current_user] = lambda: db.get(User, TUTOR_ID)
        return test_app, db

    def test_message_carries_walk_tenant_id(self):
        """Mensagem inserida deve ter tenant_id == walk.tenant_id."""
        app, db = self._build(walk_tenant_id=TENANT_ID)
        client = TestClient(app)
        r = client.post("/protected-chat/messages", json={"walk_id": WALK_ID, "body": "ola"})
        assert r.status_code == 200, r.text

        msg = db.query(ProtectedChatMessage).first()
        assert msg is not None
        assert msg.tenant_id == TENANT_ID, (
            f"Expected tenant_id={TENANT_ID!r}, got {msg.tenant_id!r}. "
            "INSERT path must propagate tenant_id from walk."
        )

    def test_message_with_null_walk_tenant_id_keeps_null(self):
        """Walk sem tenant_id (NULL) → mensagem fica com tenant_id=NULL (aceito pelo USING permissivo)."""
        app, db = self._build(walk_tenant_id=None)
        client = TestClient(app)
        r = client.post("/protected-chat/messages", json={"walk_id": WALK_ID, "body": "sem tenant"})
        assert r.status_code == 200, r.text

        msg = db.query(ProtectedChatMessage).first()
        assert msg is not None
        assert msg.tenant_id is None, (
            f"Walk with tenant_id=None should produce message with tenant_id=None, got {msg.tenant_id!r}"
        )

    def test_multiple_messages_all_carry_tenant_id(self):
        """Múltiplas mensagens (tutor e walker) devem ter tenant_id correto."""
        app, db = self._build()
        client_tutor = TestClient(app)
        app.dependency_overrides[get_current_user] = lambda: db.get(User, TUTOR_ID)
        client_tutor.post("/protected-chat/messages", json={"walk_id": WALK_ID, "body": "msg tutor"})

        app.dependency_overrides[get_current_user] = lambda: db.get(User, WALKER_ID)
        client_walker = TestClient(app)
        client_walker.post("/protected-chat/messages", json={"walk_id": WALK_ID, "body": "msg walker"})

        msgs = db.query(ProtectedChatMessage).all()
        assert len(msgs) == 2
        for m in msgs:
            assert m.tenant_id == TENANT_ID, (
                f"Message {m.id} has tenant_id={m.tenant_id!r}, expected {TENANT_ID!r}"
            )


# ---------------------------------------------------------------------------
# Part A-2: shared_walk_participants.tenant_id propagado de shared_walks.tenant_id
# ---------------------------------------------------------------------------

def _shared_walk_db():
    """DB mínimo para testes de shared_walk_service."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[
        Tenant.__table__, TenantFeature.__table__, TenantSharedWalkConfig.__table__,
        SharedWalk.__table__, SharedWalkParticipant.__table__, Pet.__table__,
        __import__("app.models.payment", fromlist=["Payment"]).Payment.__table__,
        TenantPaymentConfig.__table__,
    ])
    return sessionmaker(bind=engine)()


def _shared_tenant(db, *, with_feature=True) -> Tenant:
    t = Tenant(id="t-sw", name="SW-Tenant", slug="sw-tenant", status="active", plan="business")
    db.add(t)
    if with_feature:
        db.add(TenantFeature(tenant_id=t.id, feature_key="shared_walks", enabled=True))
    db.commit()
    return t


def _pet(db, pet_id, tutor_id, *, social=True) -> Pet:
    p = Pet(id=pet_id, tutor_id=tutor_id, name=pet_id, can_walk_with_other_pets=social)
    db.add(p)
    db.commit()
    return p


class TestSharedWalkParticipantTenantId:
    """INSERTs em shared_walk_participants devem propagar tenant_id do SharedWalk pai."""

    def test_host_participants_carry_tenant_id(self):
        """create_session: participantes do host devem ter tenant_id == shared_walk.tenant_id."""
        db = _shared_walk_db()
        t = _shared_tenant(db)
        _pet(db, "p1", "tutorA")
        _pet(db, "p2", "tutorA")

        s = svc.create_session(
            db, t, "tutorA",
            scheduled_date="2026-07-01T10:00:00",
            duration_minutes=45,
            host_pet_ids=["p1", "p2"],
            open_to_pool=False,
        )

        participants = db.query(SharedWalkParticipant).filter(
            SharedWalkParticipant.shared_walk_id == s.id
        ).all()
        assert len(participants) == 2
        for p in participants:
            assert p.tenant_id == t.id, (
                f"Host participant {p.id} has tenant_id={p.tenant_id!r}, expected {t.id!r}. "
                "create_session INSERT must propagate tenant_id from session."
            )

    def test_guest_participant_carries_tenant_id(self):
        """join_session: o participante convidado deve ter tenant_id == shared_walk.tenant_id."""
        db = _shared_walk_db()
        t = _shared_tenant(db)
        _pet(db, "p1", "tutorA")
        _pet(db, "g1", "tutorB", social=True)

        s = svc.create_session(
            db, t, "tutorA",
            scheduled_date="2026-07-01T10:00:00",
            duration_minutes=45,
            host_pet_ids=["p1"],
            open_to_pool=False,
        )
        svc.join_session(db, t, s.id, "tutorB", "g1")

        guest = (
            db.query(SharedWalkParticipant)
            .filter(
                SharedWalkParticipant.shared_walk_id == s.id,
                SharedWalkParticipant.tutor_id == "tutorB",
            )
            .first()
        )
        assert guest is not None
        assert guest.tenant_id == t.id, (
            f"Guest participant has tenant_id={guest.tenant_id!r}, expected {t.id!r}. "
            "join_session INSERT must propagate tenant_id from session."
        )

    def test_all_participants_same_tenant_id(self):
        """Todos os participantes de uma sessão devem ter o mesmo tenant_id."""
        db = _shared_walk_db()
        t = _shared_tenant(db)
        _pet(db, "p1", "tutorA")
        _pet(db, "p2", "tutorA")
        _pet(db, "g1", "tutorB", social=True)

        s = svc.create_session(
            db, t, "tutorA",
            scheduled_date="2026-07-01T10:00:00",
            duration_minutes=45,
            host_pet_ids=["p1", "p2"],
            open_to_pool=False,
        )
        svc.join_session(db, t, s.id, "tutorB", "g1")

        all_parts = db.query(SharedWalkParticipant).all()
        assert all_parts, "Deve haver participantes"
        tenant_ids = {p.tenant_id for p in all_parts}
        assert tenant_ids == {t.id}, (
            f"All participants should share tenant_id={t.id!r}, got: {tenant_ids!r}"
        )
