"""R7 — walk não-garantido até o pagamento liquidar.

Com o gate REQUIRE_PAYMENT_BEFORE_MATCHING ligado, o walk nasce 'awaiting_payment'
e só entra no fluxo operacional quando o webhook de pagamento confirmado o libera.
Default LIGADO (fail-closed — regra do dono). Aqui testamos o gate (puro) e a
liberação no webhook (que só age em walks à espera).
"""
from datetime import datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.core.database import Base, get_db, get_global_db
from app.dependencies.auth import get_current_user
from app.models.payment import Payment
from app.models.pet import Pet
from app.models.tenant import Tenant
from app.models.user import User
from app.models.walk import Walk
from app.routes import payments
from app.routes.walks import _require_payment_before_matching
from app.services.tenant_seed_service import DEFAULT_TENANT_SLUG

TENANT_ID = "t-test"
TUTOR_ID = "tutor-test"


def _build():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(Tenant(id=TENANT_ID, name="Aumigao", slug=DEFAULT_TENANT_SLUG, status="active", plan="business"))
    db.add(User(id=TUTOR_ID, email="t@x.com", password_hash="x", role="cliente", tenant_id=TENANT_ID))
    db.add(Pet(id="pet-1", tutor_id=TUTOR_ID, tenant_id=TENANT_ID, name="Bolinha"))
    db.commit()
    test_app = FastAPI()
    test_app.include_router(payments.router)
    test_app.dependency_overrides[get_db] = lambda: db
    # get_global_db e usado pelo webhook do Asaas; override para ver entidades em memoria.
    test_app.dependency_overrides[get_global_db] = lambda: db
    return test_app, db


def _local_now() -> datetime:
    # scheduled_date é hora de PAREDE local do tenant (default America/Bahia) —
    # gerar com o relógio local, como o app grava (fix fuso 08/07).
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo("America/Bahia")).replace(tzinfo=None)


def _future_iso(hours: int = 6) -> str:
    return (_local_now() + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M")


def _walk(db, op_status, scheduled_date=None):
    # Default: início bem no futuro → o corte de 45min NÃO se aplica (webhook promove).
    db.add(Walk(id="walk-1", tutor_id=TUTOR_ID, tenant_id=TENANT_ID, pet_id="pet-1",
                scheduled_date=scheduled_date or _future_iso(), duration_minutes=30, status="aguardando_pagamento",
                price=100.0, operational_status=op_status))
    db.add(Payment(id="pay-1", tenant_id=TENANT_ID, tutor_id=TUTOR_ID, amount=100.0, walk_id="walk-1",
                   status="pagamento_sandbox_criado", provider="asaas_sandbox", provider_payment_id="prov-1"))
    db.commit()


def _webhook(client, event="PAYMENT_CONFIRMED", prov="prov-1", status="CONFIRMED"):
    return client.post("/payments/webhooks/asaas",
                       json={"event": event, "payment": {"id": prov, "status": status}},
                       headers={"asaas-access-token": "segredo"})


def test_gate_default_on(monkeypatch):
    # Fail-closed: sem env explícito, o gate está LIGADO (regra do dono).
    monkeypatch.delenv("REQUIRE_PAYMENT_BEFORE_MATCHING", raising=False)
    assert _require_payment_before_matching() is True


def test_gate_can_be_disabled(monkeypatch):
    monkeypatch.setenv("REQUIRE_PAYMENT_BEFORE_MATCHING", "false")
    assert _require_payment_before_matching() is False


def test_gate_on(monkeypatch):
    monkeypatch.setenv("REQUIRE_PAYMENT_BEFORE_MATCHING", "true")
    assert _require_payment_before_matching() is True


def test_webhook_confirmed_releases_awaiting_walk(monkeypatch):
    """Sem nenhum passeador elegível na base: o webhook libera o walk E dispara o
    matching de verdade (fix 08/07 — antes ficava órfão em pending_walker_confirmation
    sem NENHUMA attempt; o passeador nunca recebia a solicitação). Sem candidato,
    o matching resolve para no_walker_found → fluxo de recovery notifica o tutor."""
    monkeypatch.setenv("ASAAS_WEBHOOK_TOKEN", "segredo")
    test_app, db = _build()
    _walk(db, op_status="awaiting_payment")
    r = _webhook(TestClient(test_app))
    assert r.status_code == 200, r.text
    db.expire_all()
    walk = db.get(Walk, "walk-1")
    # op_status no_walker_found prova que o matching RODOU (antes ficava órfão em
    # pending_walker_confirmation). Não assertamos matching_started_at aqui: sob
    # SQLite o record_operational_log interno provoca rollback implícito que
    # descarta esse campo (artefato de teste; em Postgres persiste).
    assert walk.operational_status == "no_walker_found"  # sem candidato → recovery


def test_webhook_confirmed_dispara_matching_e_cria_attempt(monkeypatch):
    """REGRESSÃO do bug do teste real 08/07: pagamento confirmado com passeador
    escolhido tem que gerar WalkMatchingAttempt pendente (a solicitação que
    aparece na fila do passeador). Antes do fix: zero attempts, passeio órfão."""
    from app.models.tenant_walker_access import TenantWalkerAccess
    from app.models.walker_profile import WalkerProfile
    from app.services.operational_matching_service import WalkMatchingAttempt

    monkeypatch.setenv("ASAAS_WEBHOOK_TOKEN", "segredo")
    test_app, db = _build()
    db.add(User(id="walker-1", email="w@x.com", password_hash="x", role="walker",
                tenant_id=TENANT_ID, is_active=True))
    db.add(WalkerProfile(id="wp-1", user_id="walker-1", status="active", active_as_walker=True))
    db.add(TenantWalkerAccess(id="twa-1", tenant_id=TENANT_ID, walker_user_id="walker-1",
                              status="active", access_type="shared_network", requirements_met=True))
    db.commit()
    db.add(Walk(id="walk-1", tutor_id=TUTOR_ID, tenant_id=TENANT_ID, pet_id="pet-1",
                scheduled_date=_future_iso(), duration_minutes=30, status="aguardando_pagamento",
                price=100.0, operational_status="awaiting_payment",
                walker_id="walker-1", assigned_walker_id="walker-1", walker_selection_mode="auto"))
    db.add(Payment(id="pay-1", tenant_id=TENANT_ID, tutor_id=TUTOR_ID, amount=100.0, walk_id="walk-1",
                   status="pagamento_sandbox_criado", provider="asaas_sandbox", provider_payment_id="prov-1"))
    db.commit()

    r = _webhook(TestClient(test_app))
    assert r.status_code == 200, r.text
    db.expire_all()
    walk = db.get(Walk, "walk-1")
    assert walk.operational_status == "pending_walker_confirmation"
    assert walk.status == "Agendado"
    assert walk.matching_started_at is not None
    attempts = db.query(WalkMatchingAttempt).filter(WalkMatchingAttempt.walk_id == "walk-1").all()
    assert len(attempts) == 1
    assert attempts[0].walker_id == "walker-1"
    assert attempts[0].status == "pending"


def test_webhook_confirmed_noop_when_walk_not_awaiting(monkeypatch):
    monkeypatch.setenv("ASAAS_WEBHOOK_TOKEN", "segredo")
    test_app, db = _build()
    _walk(db, op_status="pending_walker_confirmation")
    _webhook(TestClient(test_app))
    db.expire_all()
    # já estava no fluxo: webhook não rebaixa nem altera o operational_status
    assert db.get(Walk, "walk-1").operational_status == "pending_walker_confirmation"


def test_webhook_overdue_does_not_release_awaiting_walk(monkeypatch):
    monkeypatch.setenv("ASAAS_WEBHOOK_TOKEN", "segredo")
    test_app, db = _build()
    _walk(db, op_status="awaiting_payment")
    _webhook(TestClient(test_app), event="PAYMENT_OVERDUE", status="OVERDUE")
    db.expire_all()
    # sem liquidação, o walk continua à espera (não é garantido)
    assert db.get(Walk, "walk-1").operational_status == "awaiting_payment"


def test_webhook_race_confirmed_after_cutoff_goes_to_reconfirmation(monkeypatch):
    """Item D: pagamento confirma DEPOIS do corte (início−45min estourado) → o walk
    NÃO promove; vai para awaiting_tutor_reconfirmation com decision_reason
    'pagamento_apos_corte'. Payment segue confirmado (dinheiro rastreado)."""
    monkeypatch.setenv("ASAAS_WEBHOOK_TOKEN", "segredo")
    test_app, db = _build()
    # início a 10min de agora → dentro do corte de 45min → pós-corte.
    _walk(db, op_status="awaiting_payment",
          scheduled_date=(_local_now() + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M"))
    r = _webhook(TestClient(test_app))
    assert r.status_code == 200, r.text
    db.expire_all()
    walk = db.get(Walk, "walk-1")
    assert walk.operational_status == "awaiting_tutor_reconfirmation"
    assert walk.no_walker_reason == "pagamento_apos_corte"
    # o pagamento permanece confirmado (não estornado)
    assert db.get(Payment, "pay-1").status == "pagamento_confirmado_sandbox"


def test_webhook_race_confirmed_on_already_cancelled_walk(monkeypatch):
    """Item D: se o walk já foi cancelado por expiração (ride_cancelled) e o
    pagamento confirma depois, também vai para reconfirmação (não promove)."""
    monkeypatch.setenv("ASAAS_WEBHOOK_TOKEN", "segredo")
    test_app, db = _build()
    _walk(db, op_status="ride_cancelled")
    r = _webhook(TestClient(test_app))
    assert r.status_code == 200, r.text
    db.expire_all()
    walk = db.get(Walk, "walk-1")
    assert walk.operational_status == "awaiting_tutor_reconfirmation"
    assert walk.no_walker_reason == "pagamento_apos_corte"
