"""Testes de ROTA (camada HTTP) de app/routes/walker_quality.py.

Cobre o wiring real: response_model, auth (get_current_user), gating de
permissao (require_permission("quality.read")) nos admin_routers, e os caminhos
felizes principais de score/risco/listagem.

Monta um FastAPI minimo so com os routers do modulo + overrides de get_db /
get_current_user (SQLite em memoria) — NAO importa app.main (que conecta no Neon).

Notas de modelagem (lidas das services):
- walker_router exige user.role in {walker, passeador} E WalkerProfile existente,
  senao 403/404 (ver walker_quality_service.ensure_walker_user).
- admin_router depende de require_permission("quality.read"); usamos role
  "super_admin" (atalho de rede de seguranca em rbac.user_has_permission) para o
  caminho autorizado e um walker comum para o 403.
"""
from datetime import datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  — registra todas as tabelas no Base.metadata
from app.core.database import Base, get_db
from app.dependencies.auth import get_current_user
from app.models.user import User
from app.models.walker_incentive import WalkerIncentive
from app.models.walker_monitoring_alert import WalkerMonitoringAlert
from app.models.walker_profile import WalkerProfile
from app.routes import walker_quality

WALKER_ID = "walker-1"
ADMIN_ID = "admin-1"
TUTOR_ID = "tutor-1"


def build(*, current: str = WALKER_ID):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    # walker autenticavel com perfil aprovado
    db.add(User(id=WALKER_ID, email="walker@test.com", password_hash="x", role="walker", full_name="Joao Passeador"))
    db.add(WalkerProfile(id="wp-1", user_id=WALKER_ID, full_name="Joao Passeador", status="approved"))
    # admin (super_admin -> bypassa RBAC em user_has_permission)
    db.add(User(id=ADMIN_ID, email="admin@test.com", password_hash="x", role="super_admin"))
    # tutor comum (sem permissao quality.read, role nao-walker)
    db.add(User(id=TUTOR_ID, email="tutor@test.com", password_hash="x", role="cliente"))
    db.commit()

    test_app = FastAPI()
    test_app.include_router(walker_quality.walker_router)
    test_app.include_router(walker_quality.admin_router)
    test_app.dependency_overrides[get_db] = lambda: db
    test_app.dependency_overrides[get_current_user] = lambda: db.get(User, current)
    return TestClient(test_app), db


def set_user(client, db, user_id):
    client.app.dependency_overrides[get_current_user] = lambda: db.get(User, user_id)


# ----------------- walker_router: /walker/me -----------------
def test_my_reputation_health_happy_path():
    client, _ = build(current=WALKER_ID)
    r = client.get("/walker/me/reputation-health")
    assert r.status_code == 200, r.text
    body = r.json()
    # serializacao/response_model funciona
    assert body["risk_level"] == "normal"  # walker novo, sem reviews
    assert body["reviews_count"] == 0
    assert isinstance(body["hybrid_reputation_score"], (int, float))
    assert isinstance(body["score_breakdown"], dict)
    assert isinstance(body["recommendations"], list)
    assert isinstance(body["tip_policy"], str)
    assert body["active_recovery_plan"] is None
    assert "primeiros passeios" in body["motivational_message"]


def test_my_reputation_health_forbidden_for_non_walker():
    # role "cliente" -> ensure_walker_user levanta 403
    client, db = build(current=WALKER_ID)
    set_user(client, db, TUTOR_ID)
    r = client.get("/walker/me/reputation-health")
    assert r.status_code == 403


def test_my_reputation_health_404_without_profile():
    # walker sem WalkerProfile -> 404 (ensure_walker_user)
    client, db = build(current=WALKER_ID)
    db.add(User(id="walker-noprofile", email="np@test.com", password_hash="x", role="walker"))
    db.commit()
    set_user(client, db, "walker-noprofile")
    r = client.get("/walker/me/reputation-health")
    assert r.status_code == 404


def test_my_incentives_empty():
    client, _ = build(current=WALKER_ID)
    r = client.get("/walker/me/incentives")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {"items": [], "total": 0}


def test_my_incentives_lists_granted():
    client, db = build(current=WALKER_ID)
    db.add(WalkerIncentive(
        id="inc-1", walker_id=WALKER_ID, incentive_type="badge", title="Top",
        description="x", source="admin", status="active", visibility_effect="low",
        created_at=datetime.utcnow(),
    ))
    db.commit()
    body = client.get("/walker/me/incentives").json()
    assert body["total"] == 1
    assert body["items"][0]["id"] == "inc-1"
    assert body["items"][0]["walker_id"] == WALKER_ID


def test_my_recovery_plan_none_for_healthy_walker():
    # risk_level normal e sem force -> get_or_create_recovery_plan retorna None
    client, _ = build(current=WALKER_ID)
    r = client.get("/walker/me/recovery-plan")
    assert r.status_code == 200, r.text
    assert r.json() is None


# ----------------- admin_router gating (require_permission) -----------------
def test_admin_list_requires_permission():
    client, db = build(current=WALKER_ID)
    set_user(client, db, WALKER_ID)  # walker comum, sem quality.read
    r = client.get("/admin/walker-quality")
    assert r.status_code == 403


def test_admin_list_authorized_super_admin():
    client, db = build(current=ADMIN_ID)
    r = client.get("/admin/walker-quality")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "items" in body and "total" in body
    # o walker aprovado deve aparecer na listagem
    ids = [item["walker_id"] for item in body["items"]]
    assert WALKER_ID in ids


def test_admin_detail_requires_permission():
    client, db = build(current=WALKER_ID)
    set_user(client, db, TUTOR_ID)
    r = client.get(f"/admin/walkers/{WALKER_ID}/quality")
    assert r.status_code == 403


def test_admin_detail_happy_path():
    client, _ = build(current=ADMIN_ID)
    r = client.get(f"/admin/walkers/{WALKER_ID}/quality")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["walker"]["walker_id"] == WALKER_ID
    assert isinstance(body["score_breakdown"], dict)
    assert isinstance(body["snapshots"], list)
    assert isinstance(body["tip_policy"], str)


def test_admin_detail_404_for_unknown_walker():
    client, _ = build(current=ADMIN_ID)
    r = client.get("/admin/walkers/does-not-exist/quality")
    assert r.status_code == 404


def test_admin_list_risk_filter_excludes_normal():
    # filtro risk_level=critical sobre walker normal -> lista vazia
    client, _ = build(current=ADMIN_ID)
    r = client.get("/admin/walker-quality", params={"risk_level": "critical"})
    assert r.status_code == 200, r.text
    assert r.json()["items"] == []


def test_admin_monitoring_alerts_authorized_and_gated():
    client, db = build(current=ADMIN_ID)
    db.add(WalkerMonitoringAlert(
        id="al-1", walker_id=WALKER_ID, alert_type="low_rating", severity="medium",
        title="t", description="d", status="open", source="reputation",
        created_at=datetime.utcnow(),
    ))
    db.commit()
    body = client.get("/admin/monitoring-alerts").json()
    assert body["total"] == 1
    assert body["items"][0]["id"] == "al-1"
    # filtro por status
    assert client.get("/admin/monitoring-alerts", params={"status": "resolved"}).json()["total"] == 0
    # gating: walker comum
    set_user(client, db, WALKER_ID)
    assert client.get("/admin/monitoring-alerts").status_code == 403


def test_admin_recalculate_reputation():
    client, _ = build(current=ADMIN_ID)
    r = client.post(f"/admin/walkers/{WALKER_ID}/recalculate-reputation")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["walker_id"] == WALKER_ID
    assert body["risk_level"] == "normal"
