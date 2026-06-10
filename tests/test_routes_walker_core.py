"""Testes de ROTA (camada HTTP) do modulo app/routes/walker.py (walker_core).

Padrao do projeto (ver tests/test_routes_onda1.py e tests/test_routes_auth.py):
monta um FastAPI MINIMO so com o router de walker, SQLite em memoria
(StaticPool), overrides de get_db / get_current_user. NAO importa app.main
(que conecta no banco de PROD).

Cobre os endpoints PRINCIPAIS do passeador:
- GET /walker/profile (None quando sem perfil; reputacao/score quando existe)
- GET /walker/goals-evolution (gating _require_active_walker + payload demo)
- GET /walker/dashboard (walker_kit embutido + gating)
- GET /walker/earnings (gating + transacoes demo)
- PUT /walker/kit (atualiza submission e devolve walker_kit)
- GET /walker/public (lista publica de passeadores ativos)
- _require_active_walker: 403 quando candidatura em analise / nao liberado;
  401 quando sem autenticacao (HTTPBearer auto_error=False).
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
from app.routes import walker
from app.services.tenant_seed_service import DEFAULT_TENANT_SLUG

TENANT_ID = "t-test"
WALKER_ID = "walker-test"


def build(*, profile_kwargs: dict | None = None, role: str = "walker", create_profile: bool = True):
    """Monta app minimo com o router de walker e um SQLite em memoria isolado.

    profile_kwargs: campos do WalkerProfile (status, active_as_walker, ...).
    create_profile=False: usuario sem WalkerProfile (testa o ramo "nao encontrado").
    """
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    db.add(Tenant(id=TENANT_ID, name="Aumigao", slug=DEFAULT_TENANT_SLUG, status="active", plan="business"))
    db.add(User(id=WALKER_ID, email="walker@test.com", password_hash="x", role=role,
                tenant_id=TENANT_ID, full_name="Joao Passeador"))
    if create_profile:
        base = dict(
            id="wp-test",
            user_id=WALKER_ID,
            full_name="Joao Passeador",
            status="active",
            active_as_walker=True,
        )
        base.update(profile_kwargs or {})
        db.add(WalkerProfile(**base))
    db.commit()

    test_app = FastAPI()
    test_app.include_router(walker.router)
    test_app.include_router(walker.api_public_router)
    test_app.dependency_overrides[get_db] = lambda: db
    test_app.dependency_overrides[get_current_user] = lambda: db.get(User, WALKER_ID)
    return TestClient(test_app), db


def build_unauth():
    """App sem override de get_current_user: HTTPBearer auto_error=False -> 401."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    db.add(Tenant(id=TENANT_ID, name="Aumigao", slug=DEFAULT_TENANT_SLUG, status="active", plan="business"))
    db.commit()

    test_app = FastAPI()
    test_app.include_router(walker.router)
    test_app.dependency_overrides[get_db] = lambda: db
    return TestClient(test_app), db


# ---------------------------------------------------------------- profile ----
def test_profile_returns_none_when_no_profile():
    client, _ = build(create_profile=False)
    r = client.get("/walker/profile")
    assert r.status_code == 200, r.text
    assert r.json() is None


def test_profile_returns_data_with_reputation_and_score():
    client, _ = build(profile_kwargs={"bio": "Passeador experiente", "city": "Salvador"})
    r = client.get("/walker/profile")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == "wp-test"
    assert body["user_id"] == WALKER_ID
    assert body["status"] == "active"
    # campos de reputacao/score sao mesclados na resposta
    assert "rating_avg" in body
    assert "rating_count" in body
    assert "operational_score" in body
    assert "reliability_label" in body


def test_profile_requires_auth_401():
    client, _ = build_unauth()
    r = client.get("/walker/profile")
    assert r.status_code == 401


# ------------------------------------------------- _require_active_walker ----
def test_goals_evolution_403_when_application_under_review():
    # status submitted/under_review/approved ou active_as_walker False -> 403
    client, _ = build(profile_kwargs={"status": "submitted", "active_as_walker": False})
    r = client.get("/walker/goals-evolution")
    assert r.status_code == 403
    assert "analise" in r.json()["detail"].lower()


def test_goals_evolution_403_when_no_profile():
    client, _ = build(create_profile=False)
    r = client.get("/walker/goals-evolution")
    assert r.status_code == 403
    assert "nao encontrado" in r.json()["detail"].lower()


def test_goals_evolution_403_when_user_role_not_walker():
    # perfil ativo, mas o usuario nao tem role walker/passeador -> 403
    client, _ = build(role="cliente", profile_kwargs={"status": "active", "active_as_walker": True})
    r = client.get("/walker/goals-evolution")
    assert r.status_code == 403
    assert "liberado" in r.json()["detail"].lower()


def test_goals_evolution_happy_path_active_walker():
    client, _ = build()  # status active + active_as_walker True + role walker
    r = client.get("/walker/goals-evolution")
    assert r.status_code == 200, r.text
    body = r.json()
    # sem passeios concluidos -> fonte demo
    assert body["source"] == "demo"
    assert body["daily"]["target_walks"] == 3
    assert body["weekly"]["target_walks"] == 15
    assert body["monthly"]["target_walks"] == 60
    assert "level" in body and "current" in body["level"]


def test_goals_evolution_requires_auth_401():
    client, _ = build_unauth()
    r = client.get("/walker/goals-evolution")
    assert r.status_code == 401


# -------------------------------------------------------------- dashboard ----
def test_dashboard_happy_path_includes_walker_kit():
    client, _ = build()
    r = client.get("/walker/dashboard")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["available_requests"] == 0
    assert body["active_walks"] == 0
    # kit embutido com nivel basico por padrao (nenhum item enviado)
    kit = body["walker_kit"]
    assert kit["level"] == "basic"
    assert kit["audit_status"] == "rascunho"
    assert any(item["key"] == "water" for item in kit["items"])
    assert "goals_evolution" in body
    assert body["goals_evolution"]["source"] == "demo"


def test_dashboard_403_when_under_review():
    client, _ = build(profile_kwargs={"status": "under_review", "active_as_walker": False})
    r = client.get("/walker/dashboard")
    assert r.status_code == 403


# ---------------------------------------------------------------- earnings ----
def test_earnings_happy_path_demo_transactions():
    client, _ = build()
    r = client.get("/walker/earnings")
    assert r.status_code == 200, r.text
    body = r.json()
    # sem passeios reais -> usa transacoes demo
    assert len(body["transactions"]) == 3
    assert body["completed_walks"] == 11
    assert body["level"] == "Ouro"
    assert "available_balance" in body


def test_earnings_403_when_not_active():
    client, _ = build(profile_kwargs={"status": "approved", "active_as_walker": False})
    r = client.get("/walker/earnings")
    assert r.status_code == 403


# --------------------------------------------------------------- PUT /kit ----
def test_update_kit_marks_items_and_pending_review():
    client, db = build()
    payload = {
        "items": [
            {"key": "water", "available": True, "photo_urls": ["https://x/w.jpg"]},
            {"key": "bowl", "available": True, "photo_urls": ["https://x/b.jpg"]},
            {"key": "bags", "available": True, "photo_urls": ["https://x/g.jpg"]},
        ]
    }
    r = client.put("/walker/kit", json=payload)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    kit = body["walker_kit"]
    # 3 itens basicos disponiveis -> tier basic
    assert kit["level"] == "basic"
    assert kit["audit_status"] == "pending_review"
    water = next(item for item in kit["items"] if item["key"] == "water")
    assert water["available"] is True
    assert water["has_photo"] is True
    # persistido
    from app.models.walker_kit_submission import WalkerKitSubmission
    row = db.query(WalkerKitSubmission).filter(WalkerKitSubmission.walker_user_id == WALKER_ID).first()
    assert row is not None
    assert row.audit_status == "pending_review"


def test_update_kit_403_when_under_review():
    client, _ = build(profile_kwargs={"status": "submitted", "active_as_walker": False})
    r = client.put("/walker/kit", json={"items": []})
    assert r.status_code == 403


# ----------------------------------------------------------------- public ----
def test_public_walkers_empty_when_no_active():
    # passeador com nome "test" e filtrado por FAKE_WALKER_TOKENS; aqui usamos
    # build com perfil ativo "limpo" mas o email walker@test.com contem 'test'.
    client, _ = build()
    r = client.get("/walker/public")
    assert r.status_code == 200, r.text
    body = r.json()
    # email/identidade contem token 'test' -> filtrado por _is_public_real_walker
    assert body["walkers"] == []


def test_api_public_walkers_returns_real_walker_when_clean_identity():
    client, db = build(create_profile=False, role="walker")
    # cria usuario/perfil "limpos" (sem tokens de teste no identidade)
    db.add(User(id="clean-walker", email="maria@aumigao.app", password_hash="x",
                role="walker", tenant_id=TENANT_ID, full_name="Maria Silva"))
    db.add(WalkerProfile(id="wp-clean", user_id="clean-walker", full_name="Maria Silva",
                         cpf="11122233344", phone="71999990000", city="Salvador",
                         status="active", active_as_walker=True))
    db.commit()
    r = client.get("/api/walkers")
    assert r.status_code == 200, r.text
    rows = r.json()
    ids = [row["id"] for row in rows]
    assert "clean-walker" in ids
    row = next(row for row in rows if row["id"] == "clean-walker")
    assert row["name"] == "Maria Silva"
    assert row["verified"] is True
    assert "walker_kit" in row
