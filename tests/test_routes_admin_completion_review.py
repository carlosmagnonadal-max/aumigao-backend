"""Testes de ROTA (camada HTTP) do grupo "completion-review" de app/routes/admin.py.

Endpoints cobertos (router admin_router / api_admin_router):
- GET  /admin/walk-completions/pending           (require_permission walks.read)
- POST /admin/walk-completions/{id}/approve        (require_permission walks.update_status)
- POST /admin/walk-completions/{id}/reject         (require_permission walks.update_status)

Padrao do projeto (ver tests/test_routes_walker_quality.py e test_routes_auth.py):
monta um FastAPI MINIMO so com o admin_router de app.routes.admin + overrides de
get_db / get_current_user sobre SQLite em memoria (StaticPool). NAO importa
app.main (que conecta no Neon de PROD).

Notas de modelagem (lidas de admin.py):
- O router admin_router/api_admin_router carrega require_permission("admin.access")
  no nivel do router; cada endpoint tem permissao adicional. Um User role="super_admin"
  bypassa TODAS as permissoes (rede de seguranca em app/dependencies/rbac.user_has_permission),
  e get_admin_tenant_scope o trata como global (sem filtro de tenant). Usamos isso
  no happy path; para 403, override com role="tutor".
- Transicoes: COMPLETION_REVIEW_MUTABLE_STATUSES = {pending, pending_review, under_review}
  podem ser aprovadas/rejeitadas. Status approved -> 409 em ambas; rejected -> 409 em ambas.
- Aprovar: review.status=approved, walk.operational_status=ride_completed,
  walk.status=Finalizado, e cria Payment interno (provider=internal, status=paid).
- Rejeitar: review.status=rejected, walk.operational_status=completion_rejected.
"""
from datetime import datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 — registra todas as tabelas no Base.metadata
from app.core.database import Base, get_db
from app.dependencies.auth import get_current_user
from app.models.payment import Payment
from app.models.user import User
from app.models.walk import Walk
from app.models.walk_completion_review import WalkCompletionReview
from app.routes import admin

ADMIN_ID = "admin-1"
TUTOR_ID = "tutor-1"
WALKER_ID = "walker-1"


def build(*, current: str = ADMIN_ID):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    # super_admin -> bypassa RBAC e e tratado como escopo global (sem filtro de tenant).
    db.add(User(id=ADMIN_ID, email="admin@test.com", password_hash="x", role="super_admin", full_name="Admin"))
    # tutor comum (sem permissao) -> usado para os 403.
    db.add(User(id=TUTOR_ID, email="tutor@test.com", password_hash="x", role="tutor", full_name="Tutor"))
    db.add(User(id=WALKER_ID, email="walker@test.com", password_hash="x", role="walker", full_name="Walker"))
    db.commit()

    test_app = FastAPI()
    test_app.include_router(admin.router)
    test_app.dependency_overrides[get_db] = lambda: db
    test_app.dependency_overrides[get_current_user] = lambda: db.get(User, current)
    return TestClient(test_app), db


def set_user(client, db, user_id):
    client.app.dependency_overrides[get_current_user] = lambda: db.get(User, user_id)


def make_walk(db, walk_id="walk-1", price=50.0, status="Em andamento", op_status="ride_in_progress"):
    walk = Walk(
        id=walk_id,
        tutor_id=TUTOR_ID,
        walker_id=WALKER_ID,
        pet_id="pet-1",
        scheduled_date="2026-06-10T10:00:00",
        duration_minutes=30,
        price=price,
        status=status,
        operational_status=op_status,
    )
    db.add(walk)
    return walk


def make_review(db, review_id="rev-1", walk_id="walk-1", status="pending_review"):
    review = WalkCompletionReview(
        id=review_id,
        walk_id=walk_id,
        walker_user_id=WALKER_ID,
        tutor_user_id=TUTOR_ID,
        status=status,
        photo_url="https://example.com/p.jpg",
        notes="tudo certo",
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(review)
    return review


# ------------------------------------------------- GET /walk-completions/pending
def test_pending_empty():
    client, _ = build()
    r = client.get("/admin/walk-completions/pending")
    assert r.status_code == 200, r.text
    assert r.json() == {"items": [], "total": 0}


def test_pending_lists_only_pending_review():
    client, db = build()
    make_walk(db, "walk-1")
    make_walk(db, "walk-2")
    make_review(db, "rev-1", "walk-1", status="pending_review")
    make_review(db, "rev-2", "walk-2", status="approved")  # nao deve aparecer
    db.commit()
    body = client.get("/admin/walk-completions/pending").json()
    assert body["total"] == 1
    item = body["items"][0]
    assert item["id"] == "rev-1"
    assert item["status"] == "pending_review"
    assert item["walker_name"] == "Walker"
    assert item["tutor_name"] == "Tutor"


def test_pending_requires_permission():
    client, db = build()
    set_user(client, db, TUTOR_ID)
    r = client.get("/admin/walk-completions/pending")
    assert r.status_code == 403


# ------------------------------------------------- POST .../approve
def test_approve_happy_path_transitions_and_creates_payment():
    client, db = build()
    make_walk(db, "walk-1")
    make_review(db, "rev-1", "walk-1", status="pending_review")
    db.commit()

    r = client.post("/admin/walk-completions/rev-1/approve", json={"admin_note": "ok pela operacao"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["review"]["status"] == "approved"
    assert body["review"]["admin_note"] == "ok pela operacao"
    assert body["review"]["reviewed_by_admin_id"] == ADMIN_ID

    # transicoes persistidas no walk
    walk = db.get(Walk, "walk-1")
    assert walk.operational_status == "ride_completed"
    assert walk.status == "Finalizado"
    assert walk.matching_finished_at is not None

    # pagamento interno criado
    payment = db.query(Payment).filter(Payment.walk_id == "walk-1").first()
    assert payment is not None
    assert payment.provider == "internal"
    assert payment.status == "paid"
    assert payment.amount == 50.0
    assert payment.tutor_id == TUTOR_ID


def test_approve_does_not_duplicate_payment_when_already_paid():
    client, db = build()
    make_walk(db, "walk-1")
    make_review(db, "rev-1", "walk-1", status="pending_review")
    # pagamento ja confirmado previamente
    db.add(Payment(id="pay-pre", tutor_id=TUTOR_ID, walk_id="walk-1", amount=50.0, status="paid", provider="stripe"))
    db.commit()

    r = client.post("/admin/walk-completions/rev-1/approve")
    assert r.status_code == 200, r.text
    payments = db.query(Payment).filter(Payment.walk_id == "walk-1").all()
    assert len(payments) == 1  # nao duplicou
    assert payments[0].id == "pay-pre"


def test_approve_404_unknown_review():
    client, _ = build()
    r = client.post("/admin/walk-completions/does-not-exist/approve")
    assert r.status_code == 404


def test_approve_404_when_walk_missing():
    client, db = build()
    make_review(db, "rev-orphan", "walk-ghost", status="pending_review")  # walk inexistente
    db.commit()
    r = client.post("/admin/walk-completions/rev-orphan/approve")
    assert r.status_code == 404


def test_approve_409_when_already_approved():
    client, db = build()
    make_walk(db, "walk-1")
    make_review(db, "rev-1", "walk-1", status="approved")
    db.commit()
    r = client.post("/admin/walk-completions/rev-1/approve")
    assert r.status_code == 409


def test_approve_409_when_rejected():
    client, db = build()
    make_walk(db, "walk-1")
    make_review(db, "rev-1", "walk-1", status="rejected")
    db.commit()
    r = client.post("/admin/walk-completions/rev-1/approve")
    assert r.status_code == 409


def test_approve_requires_permission():
    client, db = build()
    make_walk(db, "walk-1")
    make_review(db, "rev-1", "walk-1", status="pending_review")
    db.commit()
    set_user(client, db, TUTOR_ID)
    r = client.post("/admin/walk-completions/rev-1/approve")
    assert r.status_code == 403


# ------------------------------------------------- POST .../reject
def test_reject_happy_path_transitions():
    client, db = build()
    make_walk(db, "walk-1")
    make_review(db, "rev-1", "walk-1", status="pending_review")
    db.commit()

    r = client.post("/admin/walk-completions/rev-1/reject", json={"reason": "evidencia insuficiente"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["review"]["status"] == "rejected"
    assert body["review"]["admin_note"] == "evidencia insuficiente"
    assert body["review"]["reviewed_by_admin_id"] == ADMIN_ID

    walk = db.get(Walk, "walk-1")
    assert walk.operational_status == "completion_rejected"
    assert walk.status == "Finalização rejeitada"

    # rejeicao NAO cria pagamento interno
    assert db.query(Payment).filter(Payment.walk_id == "walk-1").count() == 0


def test_reject_default_admin_note_when_no_payload():
    client, db = build()
    make_walk(db, "walk-1")
    make_review(db, "rev-1", "walk-1", status="pending_review")
    db.commit()
    r = client.post("/admin/walk-completions/rev-1/reject")
    assert r.status_code == 200, r.text
    assert r.json()["review"]["admin_note"]  # ha nota padrao


def test_reject_404_unknown_review():
    client, _ = build()
    r = client.post("/admin/walk-completions/nope/reject")
    assert r.status_code == 404


def test_reject_409_when_already_rejected():
    client, db = build()
    make_walk(db, "walk-1")
    make_review(db, "rev-1", "walk-1", status="rejected")
    db.commit()
    r = client.post("/admin/walk-completions/rev-1/reject")
    assert r.status_code == 409


def test_reject_409_when_already_approved():
    client, db = build()
    make_walk(db, "walk-1")
    make_review(db, "rev-1", "walk-1", status="approved")
    db.commit()
    r = client.post("/admin/walk-completions/rev-1/reject")
    assert r.status_code == 409


def test_reject_requires_permission():
    client, db = build()
    make_walk(db, "walk-1")
    make_review(db, "rev-1", "walk-1", status="pending_review")
    db.commit()
    set_user(client, db, TUTOR_ID)
    r = client.post("/admin/walk-completions/rev-1/reject")
    assert r.status_code == 403
