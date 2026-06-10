"""Testes de ROTA (camada HTTP) para app/routes/reviews.py.

Cobre o wiring real dos endpoints de avaliacao/reputacao: criacao de review
(com gating: exige finalizacao operacional aprovada + completion review approved),
listagem publica de reviews de um passeador, e o gating de permissao dos endpoints
admin (require_permission("reviews.read")). Tambem cobre bordas de rating (1..5,
fora do range -> 422 da validacao Pydantic).

Monta um FastAPI MINIMO so com os routers do modulo + overrides de get_db /
get_current_user (SQLite em memoria) — NAO importa app.main (que conecta no Neon).
"""
from datetime import datetime
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  — registra todas as tabelas no Base.metadata
from app.core.database import Base, get_db
from app.dependencies.auth import get_current_user
from app.models.pet import Pet
from app.models.tenant import Tenant
from app.models.user import User
from app.models.walk import Walk
from app.models.walk_completion_review import WalkCompletionReview
from app.models.walk_review import WalkReview
from app.models.walker_review import WalkerReview
from app.routes import reviews

TENANT_ID = "t-test"
TUTOR_ID = "tutor-test"
WALKER_ID = "walker-test"
ADMIN_ID = "admin-test"


def _seed(db):
    db.add(Tenant(id=TENANT_ID, name="Aumigao", slug="aumigao", status="active", plan="business"))
    db.add(User(id=TUTOR_ID, email="tutor@test.com", password_hash="x", role="cliente", tenant_id=TENANT_ID, full_name="Maria Silva"))
    db.add(User(id=WALKER_ID, email="walker@test.com", password_hash="x", role="passeador", tenant_id=TENANT_ID, full_name="Joao Passeador"))
    # super_admin: a rede de seguranca do RBAC deixa passar require_permission sem seed de papeis.
    db.add(User(id=ADMIN_ID, email="admin@test.com", password_hash="x", role="super_admin", tenant_id=TENANT_ID, full_name="Admin"))
    db.add(Pet(id="rex", tutor_id=TUTOR_ID, name="Rex"))
    db.commit()


def build(*, actor_id: str = TUTOR_ID):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    _seed(db)

    test_app = FastAPI()
    test_app.include_router(reviews.walks_router)
    test_app.include_router(reviews.walkers_router)
    test_app.include_router(reviews.walker_router)
    test_app.include_router(reviews.admin_router)

    state = {"uid": actor_id}
    test_app.dependency_overrides[get_db] = lambda: db
    test_app.dependency_overrides[get_current_user] = lambda: db.get(User, state["uid"])
    client = TestClient(test_app)
    client.state_dict = state  # permite trocar o usuario autenticado nos testes
    return client, db


def _make_walk(db, *, operational_status="ride_completed", walker_id=WALKER_ID, tutor_id=TUTOR_ID):
    walk = Walk(
        id=str(uuid4()),
        tutor_id=tutor_id,
        tenant_id=TENANT_ID,
        walker_id=walker_id,
        pet_id="rex",
        scheduled_date="2026-06-01T10:00",
        duration_minutes=45,
        price=50.0,
        status="Finalizado",
        operational_status=operational_status,
    )
    db.add(walk)
    db.commit()
    return walk


def _approve_completion(db, walk):
    db.add(WalkCompletionReview(
        id=str(uuid4()),
        tenant_id=TENANT_ID,
        walk_id=walk.id,
        walker_user_id=WALKER_ID,
        tutor_user_id=TUTOR_ID,
        status="approved",
        reviewed_at=datetime.utcnow(),
    ))
    db.commit()


# ---------------- criar review: HAPPY PATH ----------------
def test_create_review_happy_path():
    client, db = build()
    walk = _make_walk(db)
    _approve_completion(db, walk)
    r = client.post(f"/walks/{walk.id}/review", json={"rating": 5, "comment": "Excelente!", "tags": ["punctual", "caring"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["review"]["rating"] == 5
    assert body["review"]["walker_id"] == WALKER_ID
    assert body["review"]["tutor_id"] == TUTOR_ID
    assert sorted(body["review"]["tags"]) == ["caring", "punctual"]
    # persistiu de fato
    assert db.query(WalkReview).filter(WalkReview.walk_id == walk.id).count() == 1


def test_create_review_drops_unknown_tags():
    client, db = build()
    walk = _make_walk(db)
    _approve_completion(db, walk)
    r = client.post(f"/walks/{walk.id}/review", json={"rating": 4, "tags": ["punctual", "lixo_invalido"]})
    assert r.status_code == 200, r.text
    assert r.json()["review"]["tags"] == ["punctual"]


# ---------------- criar review: GATING ----------------
def test_create_review_requires_approved_completion_review():
    """operational_status ja eh ride_completed, mas falta o completion review aprovado."""
    client, db = build()
    walk = _make_walk(db, operational_status="ride_completed")
    # sem WalkCompletionReview approved
    r = client.post(f"/walks/{walk.id}/review", json={"rating": 5})
    assert r.status_code == 409
    assert "revis" in r.json()["detail"].lower()


def test_create_review_requires_operational_completion():
    """operational_status diferente de ride_completed bloqueia mesmo com tutor dono."""
    client, db = build()
    walk = _make_walk(db, operational_status="ride_scheduled")
    _approve_completion(db, walk)
    r = client.post(f"/walks/{walk.id}/review", json={"rating": 5})
    assert r.status_code == 409
    assert "finaliza" in r.json()["detail"].lower()


def test_create_review_only_tutor_owner():
    """Walker do passeio tem acesso (passa _get_walk_for_user) mas nao pode avaliar -> 403."""
    client, db = build(actor_id=WALKER_ID)
    walk = _make_walk(db)
    _approve_completion(db, walk)
    r = client.post(f"/walks/{walk.id}/review", json={"rating": 5})
    assert r.status_code == 403


def test_create_review_duplicate_conflict():
    client, db = build()
    walk = _make_walk(db)
    _approve_completion(db, walk)
    first = client.post(f"/walks/{walk.id}/review", json={"rating": 5})
    assert first.status_code == 200, first.text
    second = client.post(f"/walks/{walk.id}/review", json={"rating": 4})
    assert second.status_code == 409
    assert "avalia" in second.json()["detail"].lower()


def test_create_review_walk_not_found():
    client, db = build()
    r = client.post("/walks/inexistente/review", json={"rating": 5})
    assert r.status_code == 404


# ---------------- bordas de rating (validacao Pydantic ge=1 le=5) ----------------
def test_create_review_rating_zero_rejected():
    client, db = build()
    walk = _make_walk(db)
    _approve_completion(db, walk)
    r = client.post(f"/walks/{walk.id}/review", json={"rating": 0})
    assert r.status_code == 422


def test_create_review_rating_above_five_rejected():
    client, db = build()
    walk = _make_walk(db)
    _approve_completion(db, walk)
    r = client.post(f"/walks/{walk.id}/review", json={"rating": 6})
    assert r.status_code == 422


def test_create_review_rating_one_accepted():
    client, db = build()
    walk = _make_walk(db)
    _approve_completion(db, walk)
    r = client.post(f"/walks/{walk.id}/review", json={"rating": 1})
    assert r.status_code == 200, r.text
    assert r.json()["review"]["rating"] == 1


# ---------------- listar reviews de um passeador (publico, exige auth) ----------------
def test_walker_reviews_empty():
    client, db = build()
    r = client.get(f"/walkers/{WALKER_ID}/reviews")
    assert r.status_code == 200
    body = r.json()
    assert body == {"items": [], "total": 0}


def test_walker_reviews_lists_and_anonymizes_tutor():
    client, db = build()
    db.add(WalkerReview(
        id=str(uuid4()),
        walk_id="w-1",
        tutor_id=TUTOR_ID,
        walker_id=WALKER_ID,
        rating=5,
        comment="Otimo passeio",
    ))
    db.commit()
    r = client.get(f"/walkers/{WALKER_ID}/reviews")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 1
    item = body["items"][0]
    assert item["rating"] == 5
    assert item["comment"] == "Otimo passeio"
    # anonimizacao: primeiro nome + inicial ("Maria Silva" -> "Maria A.")
    assert item["tutor_name"] == "Maria A."


# ---------------- AUTH: sem usuario autenticado -> 401 ----------------
def test_walker_reviews_requires_auth():
    # Remove o override de get_current_user para exercer o auth REAL (HTTPBearer):
    # sem header Authorization, deve retornar 401. (get_current_user so usa db+header,
    # nao conecta em prod.)
    client, db = build()
    del client.app.dependency_overrides[get_current_user]
    r = client.get(f"/walkers/{WALKER_ID}/reviews")
    assert r.status_code == 401


def test_create_review_requires_auth():
    client, db = build()
    walk = _make_walk(db)
    del client.app.dependency_overrides[get_current_user]
    r = client.post(f"/walks/{walk.id}/review", json={"rating": 5})
    assert r.status_code == 401


# ---------------- admin: gating de permissao ----------------
def test_admin_reputation_list_forbidden_for_tutor():
    client, db = build(actor_id=TUTOR_ID)
    r = client.get("/admin/walkers/reputation")
    assert r.status_code == 403


def test_admin_reputation_list_ok_for_super_admin():
    client, db = build(actor_id=ADMIN_ID)
    r = client.get("/admin/walkers/reputation")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "items" in body and "total" in body


def test_admin_flag_review_forbidden_for_tutor():
    client, db = build(actor_id=TUTOR_ID)
    db.add(WalkerReview(id="rv-1", walk_id="w-1", tutor_id=TUTOR_ID, walker_id=WALKER_ID, rating=2))
    db.commit()
    r = client.patch("/admin/reviews/rv-1/flag", json={"is_flagged": True, "admin_notes": "abuso"})
    assert r.status_code == 403


def test_admin_flag_review_ok_for_super_admin():
    client, db = build(actor_id=ADMIN_ID)
    db.add(WalkerReview(id="rv-1", walk_id="w-1", tutor_id=TUTOR_ID, walker_id=WALKER_ID, rating=2))
    db.commit()
    r = client.patch("/admin/reviews/rv-1/flag", json={"is_flagged": True, "admin_notes": "abuso"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["is_flagged"] is True
    assert body["admin_notes"] == "abuso"
    db.expire_all()
    assert db.get(WalkerReview, "rv-1").is_flagged is True
