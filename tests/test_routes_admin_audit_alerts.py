"""Testes de ROTA (camada HTTP) do grupo audit-alertas de app/routes/admin.py.

Cobre o wiring real dos endpoints de governanca/observabilidade do admin:
- GET  /admin/audit-logs        -> require_permission("audit_logs.read")
- GET  /admin/operational-events  (gating so do router: admin.access)
- POST /admin/operational-events -> require_permission("alerts.resolve")
- GET  /admin/operational-alerts  (gating so do router: admin.access)

Monta um FastAPI MINIMO so com o router de admin + overrides de get_db /
get_current_user (SQLite em memoria StaticPool) — NAO importa app.main (Neon PROD).

Padrao (ver tests/test_routes_walker_quality.py e tests/test_routes_auth.py):
- import app.models antes de create_all (registra tabelas).
- super_admin passa em qualquer require_permission (rede de seguranca em
  app/dependencies/rbac.user_has_permission) -> caminho autorizado.
- tutor comum nao tem admin.access (dependency do proprio router) -> 403.

Notas de modelagem (lidas do codigo):
- O router admin tem dependency de nivel `require_permission("admin.access")`
  (admin.py:56-57); por isso um tutor toma 403 ate em endpoints sem perm
  especifica (ex.: operational-alerts, operational-events).
- POST /operational-events tambem grava um AuditLog espelhado via
  record_admin_operational_event -> record_audit_log (com tenant_id=None pois
  nao ha Request); o super_admin tem escopo global e enxerga esse log.
- get_admin_tenant_scope: super_admin -> escopo global (ve todos os tenants);
  admin regular -> filtra pelo proprio tenant_id (admin.py usa apply_tenant_filter).
"""
from datetime import datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 - registra todas as tabelas no Base.metadata
from app.core.database import Base, get_db
from app.dependencies.auth import get_current_user
from app.models.admin_operational_event import AdminOperationalEvent
from app.models.audit_log import AuditLog
from app.models.user import User
from app.routes import admin

SUPER_ID = "super-1"
TUTOR_ID = "tutor-1"
ADMIN_T1_ID = "admin-t1"
TENANT_1 = "tenant-1"
TENANT_2 = "tenant-2"


def build(*, current: str = SUPER_ID):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    # super_admin -> bypassa RBAC + escopo global de tenant.
    db.add(User(id=SUPER_ID, email="super@test.com", password_hash="x", role="super_admin"))
    # tutor comum -> sem admin.access (gating do proprio router).
    db.add(User(id=TUTOR_ID, email="tutor@test.com", password_hash="x", role="tutor"))
    # admin regular vinculado ao TENANT_1 (escopo restrito ao proprio tenant).
    db.add(User(id=ADMIN_T1_ID, email="admin1@test.com", password_hash="x",
                role="admin", tenant_id=TENANT_1))
    db.commit()

    test_app = FastAPI()
    test_app.include_router(admin.router)
    test_app.dependency_overrides[get_db] = lambda: db
    test_app.dependency_overrides[get_current_user] = lambda: db.get(User, current)
    return TestClient(test_app), db


def set_user(client, db, user_id):
    client.app.dependency_overrides[get_current_user] = lambda: db.get(User, user_id)


def seed_audit_log(db, **kw):
    base = dict(
        id=kw.get("id"),
        actor_user_id=kw.get("actor_user_id", SUPER_ID),
        actor_type=kw.get("actor_type", "admin"),
        tenant_id=kw.get("tenant_id"),
        action=kw.get("action", "walker.approved"),
        entity_type=kw.get("entity_type", "walker"),
        entity_id=kw.get("entity_id", "w-1"),
        created_at=kw.get("created_at", datetime.utcnow()),
    )
    log = AuditLog(**base)
    db.add(log)
    db.commit()
    return log


# ----------------------------------------------------------- audit-logs ------
def test_audit_logs_happy_path_super_admin():
    client, db = build(current=SUPER_ID)
    seed_audit_log(db, id="al-1", action="walker.approved", entity_id="w-1")
    r = client.get("/admin/audit-logs")
    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body, list)
    assert len(body) == 1
    item = body[0]
    # response shape (campos mapeados na rota)
    assert item["id"] == "al-1"
    assert item["action"] == "walker.approved"
    assert item["entity_type"] == "walker"
    assert item["entity_id"] == "w-1"
    assert item["actor_user_id"] == SUPER_ID
    assert "created_at" in item and "before_data" in item and "after_data" in item


def test_audit_logs_forbidden_for_tutor():
    # tutor nao tem admin.access (dependency do router) nem audit_logs.read.
    client, db = build(current=TUTOR_ID)
    seed_audit_log(db, id="al-x")
    r = client.get("/admin/audit-logs")
    assert r.status_code == 403


def test_audit_logs_ordered_desc_by_created_at():
    client, db = build(current=SUPER_ID)
    now = datetime.utcnow()
    seed_audit_log(db, id="old", created_at=now - timedelta(hours=2), action="a.old")
    seed_audit_log(db, id="new", created_at=now, action="a.new")
    body = client.get("/admin/audit-logs").json()
    assert [i["id"] for i in body] == ["new", "old"]


def test_audit_logs_limit_query_validation():
    client, db = build(current=SUPER_ID)
    # limit fora do range [1, 500] -> 422 (Query ge=1 le=500)
    assert client.get("/admin/audit-logs", params={"limit": 0}).status_code == 422
    assert client.get("/admin/audit-logs", params={"limit": 501}).status_code == 422
    # dentro do range -> 200
    assert client.get("/admin/audit-logs", params={"limit": 5}).status_code == 200


def test_audit_logs_limit_caps_returned_rows():
    client, db = build(current=SUPER_ID)
    for i in range(4):
        seed_audit_log(db, id=f"al-{i}", created_at=datetime.utcnow() + timedelta(seconds=i))
    body = client.get("/admin/audit-logs", params={"limit": 2}).json()
    assert len(body) == 2


def test_audit_logs_regular_admin_without_rbac_seed_forbidden():
    # admin regular (role="admin") NAO bypassa RBAC: sem seed de audit_logs.read,
    # toma 403. Confirma que o atalho de super_admin nao se estende a role="admin".
    # (o filtro de tenant via apply_tenant_filter so e exercitado com RBAC seed,
    #  setup fora do escopo deste teste minimo — ver notes.)
    client, db = build(current=ADMIN_T1_ID)
    seed_audit_log(db, id="mine", tenant_id=TENANT_1)
    r = client.get("/admin/audit-logs")
    assert r.status_code == 403
    assert r.json()["detail"] == "Permissao negada"


# --------------------------------------------------- operational-events ------
def test_list_operational_events_empty():
    client, _ = build(current=SUPER_ID)
    r = client.get("/admin/operational-events")
    assert r.status_code == 200, r.text
    assert r.json() == {"items": [], "total": 0}


def test_list_operational_events_forbidden_for_tutor():
    client, _ = build(current=TUTOR_ID)
    assert client.get("/admin/operational-events").status_code == 403


def test_list_operational_events_filters():
    client, db = build(current=SUPER_ID)
    db.add(AdminOperationalEvent(
        id="ev-low", event_type="admin_note_added", entity_type="walk",
        entity_id="wk-1", severity="info", title="t", description="d",
        source="admin-web", metadata_json="{}", created_at=datetime.utcnow(),
    ))
    db.add(AdminOperationalEvent(
        id="ev-high", event_type="walk_recovered", entity_type="walk",
        entity_id="wk-2", severity="high", title="t2", description="d2",
        source="admin-web", metadata_json="{}", created_at=datetime.utcnow(),
    ))
    db.commit()
    # sem filtro: ambos
    assert client.get("/admin/operational-events").json()["total"] == 2
    # filtro por severity
    high = client.get("/admin/operational-events", params={"severity": "high"}).json()
    assert high["total"] == 1 and high["items"][0]["id"] == "ev-high"
    # filtro por entity_id
    one = client.get("/admin/operational-events", params={"entity_id": "wk-1"}).json()
    assert one["total"] == 1 and one["items"][0]["id"] == "ev-low"


def test_create_operational_event_happy_path():
    client, db = build(current=SUPER_ID)
    r = client.post("/admin/operational-events", json={
        "entity_type": "walk",
        "entity_id": "wk-99",
        "title": "Intervencao manual",
        "event_type": "admin_note_added",
        "severity": "warning",
        "description": "ajuste",
        "metadata": {"k": "v"},
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["entity_type"] == "walk"
    assert body["entity_id"] == "wk-99"
    assert body["title"] == "Intervencao manual"
    assert body["severity"] == "warning"
    assert body["actor_user_id"] == SUPER_ID
    assert body["metadata"] == {"k": "v"}
    # persistido
    assert db.query(AdminOperationalEvent).filter_by(entity_id="wk-99").count() == 1


def test_create_operational_event_writes_mirrored_audit_log():
    # record_admin_operational_event grava um AuditLog espelhado (action=walk.<event>).
    client, db = build(current=SUPER_ID)
    client.post("/admin/operational-events", json={
        "entity_type": "walk", "entity_id": "wk-7", "title": "x",
        "event_type": "admin_note_added",
    })
    logs = client.get("/admin/audit-logs").json()
    actions = [l["action"] for l in logs]
    assert "walk.admin_note_added" in actions


def test_create_operational_event_forbidden_for_tutor():
    # tutor: barra ja no admin.access do router (antes de alerts.resolve).
    client, _ = build(current=TUTOR_ID)
    r = client.post("/admin/operational-events", json={
        "entity_type": "walk", "entity_id": "wk-1", "title": "t",
    })
    assert r.status_code == 403


def test_create_operational_event_rejects_invalid_entity_type():
    client, _ = build(current=SUPER_ID)
    r = client.post("/admin/operational-events", json={
        "entity_type": "nao_existe", "entity_id": "x", "title": "t",
    })
    assert r.status_code == 400
    assert "entity_type" in r.json()["detail"]


def test_create_operational_event_requires_entity_id_and_title():
    client, _ = build(current=SUPER_ID)
    r1 = client.post("/admin/operational-events", json={
        "entity_type": "walk", "entity_id": "", "title": "t",
    })
    assert r1.status_code == 400
    assert "entity_id" in r1.json()["detail"]
    r2 = client.post("/admin/operational-events", json={
        "entity_type": "walk", "entity_id": "x", "title": "",
    })
    assert r2.status_code == 400
    assert "title" in r2.json()["detail"]


# --------------------------------------------------- operational-alerts ------
def test_operational_alerts_empty_happy_path():
    client, _ = build(current=SUPER_ID)
    r = client.get("/admin/operational-alerts")
    assert r.status_code == 200, r.text
    assert r.json() == {"total": 0, "items": []}


def test_operational_alerts_forbidden_for_tutor():
    # gating do router (admin.access) cobre este endpoint sem perm especifica.
    client, _ = build(current=TUTOR_ID)
    assert client.get("/admin/operational-alerts").status_code == 403
