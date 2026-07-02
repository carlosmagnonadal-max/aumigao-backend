"""Testes de ROTA (camada HTTP) do modulo app/routes/matching.py.

Padrao do projeto (ver tests/test_routes_onda1.py e tests/test_routes_auth.py):
monta um FastAPI MINIMO so com o router de matching, SQLite em memoria (StaticPool),
overrides de get_db / get_current_user. NAO importa app.main (que conecta no Neon).

Cobre:
- POST /matching/walkers (happy path: estrutura MatchingResponse; sem passeadores;
  401 sem auth).
- GET /admin/matching/diagnostics (estrutura MatchingDebugResponse; RBAC 403 para usuario
  comum, 200 para super_admin).
- GET /admin/matching/boosts (lista + total; RBAC).
- PATCH /admin/matching/boosts/{walker_id} (cria/atualiza boost; elegibilidade).

NAO asserta rotulos de nivel (apenas estrutura), conforme instrucao do alvo.
"""
from datetime import datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 - registra todas as tabelas no Base.metadata
from app.core.database import Base, get_db
from app.dependencies.auth import get_current_user
from app.models.tenant import Tenant
from app.models.user import User
from app.models.walker_profile import WalkerProfile
from app.routes import matching
from app.services.tenant_seed_service import DEFAULT_TENANT_SLUG

TENANT_ID = "t-test"
TUTOR_ID = "tutor-test"
ADMIN_ID = "admin-test"


def build(*, role="cliente"):
    """Monta app minimo com o router de matching e um SQLite em memoria isolado.

    O usuario autenticado por padrao e um cliente (TUTOR_ID). Para as rotas admin
    (require_permission('matching.read')), troca-se o override para um super_admin,
    que passa sem precisar de seed de RBAC.
    """
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    db.add(Tenant(id=TENANT_ID, name="Aumigao", slug=DEFAULT_TENANT_SLUG, status="active", plan="business"))
    db.add(User(id=TUTOR_ID, email="tutor@test.com", password_hash="x", role="cliente", tenant_id=TENANT_ID))
    db.add(User(id=ADMIN_ID, email="admin@test.com", password_hash="x", role="super_admin", tenant_id=TENANT_ID))
    db.commit()

    test_app = FastAPI()
    test_app.include_router(matching.router)
    test_app.include_router(matching.admin_router)
    test_app.dependency_overrides[get_db] = lambda: db
    test_app.dependency_overrides[get_current_user] = lambda: db.get(User, TUTOR_ID)

    client = TestClient(test_app)
    if role == "super_admin":
        client.app.dependency_overrides[get_current_user] = lambda: db.get(User, ADMIN_ID)
    return client, db


def add_walker(db, *, user_id, status="active", active_as_walker=True, city="salvador",
               full_name="Passeador X", has_vehicle=False):
    db.add(User(id=user_id, email=f"{user_id}@test.com", password_hash="x", role="walker", tenant_id=TENANT_ID))
    db.add(WalkerProfile(
        id=f"profile-{user_id}",
        user_id=user_id,
        full_name=full_name,
        status=status,
        active_as_walker=active_as_walker,
        has_vehicle=has_vehicle,
        city=city,
        created_at=datetime.utcnow(),
    ))
    # C11: a preview de matching agora restringe ao pool da rede do tenant; o walker
    # precisa de vínculo ativo (TenantWalkerAccess) com o TENANT_ID para ser elegível.
    from app.models.tenant_walker_access import TenantWalkerAccess
    db.add(TenantWalkerAccess(id=f"twa-{user_id}", tenant_id=TENANT_ID, walker_user_id=user_id,
                              status="active", access_type="shared_network"))
    db.commit()


# ------------------------------------------------------- POST /matching/walkers
def test_match_walkers_empty_returns_structure():
    client, _ = build()
    r = client.post("/matching/walkers", json={"duration_minutes": 45})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["top_recommended"] == []
    assert body["other_options"] == []
    assert body["total_found"] == 0
    assert body["matching_context"]["duration_minutes"] == 45


def test_match_walkers_happy_path_returns_eligible_walker():
    client, db = build()
    add_walker(db, user_id="walker-a", city="salvador")
    r = client.post("/matching/walkers", json={"city": "salvador", "duration_minutes": 45})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total_found"] == 1
    found = body["top_recommended"] + body["other_options"]
    ids = {w["walker_id"] for w in found}
    assert "walker-a" in ids
    # Estrutura publica do passeador (sem assertar rotulo de nivel especifico).
    w = found[0]
    for key in ("walker_id", "name", "rating_average", "reviews_count", "total_walks",
                "level", "badges", "display_reason", "can_select"):
        assert key in w


def test_match_walkers_ignores_inactive_and_pending_walkers():
    client, db = build()
    # Nao elegivel: status != active.
    add_walker(db, user_id="walker-pending", status="pending")
    # Nao elegivel: active_as_walker False.
    add_walker(db, user_id="walker-off", status="active", active_as_walker=False)
    r = client.post("/matching/walkers", json={"duration_minutes": 45})
    assert r.status_code == 200, r.text
    assert r.json()["total_found"] == 0


def test_match_walkers_requires_auth_401():
    client, _ = build()
    # Remove o override -> get_current_user real -> HTTPBearer auto_error=False -> 401.
    client.app.dependency_overrides.pop(get_current_user, None)
    r = client.post("/matching/walkers", json={"duration_minutes": 45})
    assert r.status_code == 401


# ------------------------------------------------- GET /admin/matching/diagnostics ---
def test_debug_forbidden_for_regular_user_403():
    client, db = build()  # usuario autenticado = cliente comum
    add_walker(db, user_id="walker-a")
    r = client.get("/admin/matching/diagnostics")
    assert r.status_code == 403
    assert "permiss" in r.json()["detail"].lower()


def test_debug_returns_scored_items_for_admin():
    client, db = build(role="super_admin")
    add_walker(db, user_id="walker-a", city="salvador")
    r = client.get("/admin/matching/diagnostics", params={"city": "salvador"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total_found"] == 1
    item = body["items"][0]
    # Campos de debug (scores) presentes na estrutura.
    for key in ("proximity_score", "rating_score", "experience_score",
                "availability_score", "matching_score_base", "behavior_score",
                "boost_score", "final_matching_score"):
        assert key in item


# ------------------------------------------------- GET /admin/matching/boosts --
def test_list_boosts_forbidden_for_regular_user_403():
    client, _ = build()
    r = client.get("/admin/matching/boosts")
    assert r.status_code == 403


def test_list_boosts_returns_items_and_total():
    client, db = build(role="super_admin")
    add_walker(db, user_id="walker-a")
    add_walker(db, user_id="walker-b")
    r = client.get("/admin/matching/boosts")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 2
    assert len(body["items"]) == 2
    item = body["items"][0]
    for key in ("walker_id", "walker_name", "boost_enabled", "boost_score",
                "boost_status", "can_apply_boost", "eligibility_reason"):
        assert key in item


def test_list_boosts_filter_by_status():
    client, db = build(role="super_admin")
    add_walker(db, user_id="walker-a", status="active")
    add_walker(db, user_id="walker-approved", status="approved")
    r = client.get("/admin/matching/boosts", params={"status": "approved"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["walker_id"] == "walker-approved"


# ---------------------------------------- PATCH /admin/matching/boosts/{id} ----
def test_update_boost_forbidden_for_regular_user_403():
    client, db = build()
    add_walker(db, user_id="walker-a", status="approved")
    r = client.patch("/admin/matching/boosts/walker-a", json={"boost_enabled": True, "boost_score": 3})
    assert r.status_code == 403


def test_update_boost_enables_for_approved_walker():
    client, db = build(role="super_admin")
    # Elegibilidade de boost exige status == approved e sem reviews ruins.
    add_walker(db, user_id="walker-a", status="approved")
    r = client.patch("/admin/matching/boosts/walker-a", json={
        "boost_enabled": True,
        "boost_score": 4,
        "boost_reason": "destaque manual",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["walker_id"] == "walker-a"
    assert body["boost_enabled"] is True
    assert body["boost_score"] == 4
    assert body["boost_status"] == "active"
    # Passeador approved sem reviews ruins => elegivel.
    assert body["can_apply_boost"] is True


def test_update_boost_eligibility_false_for_non_approved():
    client, db = build(role="super_admin")
    # status active (nao approved) => boost nao elegivel, mas a rota ainda responde 200.
    add_walker(db, user_id="walker-a", status="active")
    r = client.patch("/admin/matching/boosts/walker-a", json={"boost_enabled": True, "boost_score": 2})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["can_apply_boost"] is False
    assert "approved" in body["eligibility_reason"].lower()


def test_update_boost_creates_profile_when_missing():
    client, db = build(role="super_admin")
    # walker_id sem WalkerProfile: a rota cria um profile pending automaticamente.
    db.add(User(id="ghost", email="ghost@test.com", password_hash="x", role="walker", tenant_id=TENANT_ID))
    db.commit()
    r = client.patch("/admin/matching/boosts/ghost", json={"boost_enabled": False, "boost_score": 0})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["walker_id"] == "ghost"
    assert body["status"] == "pending"
    created = db.query(WalkerProfile).filter(WalkerProfile.user_id == "ghost").first()
    assert created is not None


def test_update_boost_score_clamped_to_max_5():
    client, db = build(role="super_admin")
    add_walker(db, user_id="walker-a", status="approved")
    # Pydantic valida boost_score <= 5; valor invalido -> 422.
    r = client.patch("/admin/matching/boosts/walker-a", json={"boost_enabled": True, "boost_score": 99})
    assert r.status_code == 422
