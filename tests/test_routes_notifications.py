"""Testes de ROTA (camada HTTP) do modulo app/routes/notifications.py.

Monta um FastAPI MINIMO so com o router de notifications + overrides de get_db /
get_current_user (SQLite em memoria, StaticPool) — NAO importa app.main (que
conecta no banco de PROD).

Cobre:
- listar notificacoes do usuario (happy path)
- filtro only_unread
- unread-count
- marcar como lida (PATCH /{id}/read) + read-all
- 401 sem auth (usando o get_current_user real)
- 403 (criar notificacao manual sem ser admin; marcar notificacao de outro usuario)
- isolamento por usuario (tutor A nao ve/nao marca notificacao do tutor B)
- isolamento por tenant
"""
import json
import uuid
from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  — registra todas as tabelas no Base.metadata
from app.core.database import Base, get_db
from app.dependencies.auth import get_current_user
from app.models.notification import Notification
from app.models.tenant import Tenant
from app.models.user import User
from app.routes import notifications
from app.services.tenant_seed_service import DEFAULT_TENANT_SLUG

TENANT_ID = "t-test"
OTHER_TENANT_ID = "t-other"
TUTOR_A = "tutor-a"
TUTOR_B = "tutor-b"
ADMIN_ID = "admin-1"


def _add_notification(
    db,
    *,
    notif_id=None,
    tenant_id=TENANT_ID,
    user_id=TUTOR_A,
    user_role="tutor",
    title="Titulo",
    message="Mensagem",
    ntype="info",
    is_read=False,
    metadata=None,
):
    n = Notification(
        id=notif_id or str(uuid.uuid4()),
        tenant_id=tenant_id,
        user_id=user_id,
        user_role=user_role,
        title=title,
        message=message,
        type=ntype,
        metadata_json=json.dumps(metadata or {}),
        is_read=is_read,
        created_at=datetime.utcnow(),
    )
    db.add(n)
    db.commit()
    return n


def build(*, current=TUTOR_A, with_real_auth=False):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    # slug = DEFAULT para resolve_current_tenant_id resolver este tenant sem criar outro.
    db.add(Tenant(id=TENANT_ID, name="Aumigao", slug=DEFAULT_TENANT_SLUG, status="active", plan="business"))
    db.add(Tenant(id=OTHER_TENANT_ID, name="Outro", slug="outro", status="active", plan="business"))
    db.add(User(id=TUTOR_A, email="a@test.com", password_hash="x", role="cliente", tenant_id=TENANT_ID))
    db.add(User(id=TUTOR_B, email="b@test.com", password_hash="x", role="cliente", tenant_id=TENANT_ID))
    db.add(User(id=ADMIN_ID, email="admin@test.com", password_hash="x", role="admin", tenant_id=TENANT_ID))
    db.commit()

    test_app = FastAPI()
    test_app.include_router(notifications.router)
    test_app.dependency_overrides[get_db] = lambda: db
    if not with_real_auth:
        test_app.dependency_overrides[get_current_user] = lambda: db.get(User, current)
    return TestClient(test_app), db


# ----- happy path: listar -----
def test_list_notifications_returns_user_notifications():
    client, db = build(current=TUTOR_A)
    _add_notification(db, title="N1")
    _add_notification(db, title="N2")
    r = client.get("/notifications")
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body) == 2
    titles = sorted(n["title"] for n in body)
    assert titles == ["N1", "N2"]
    # serializacao: metadata vira dict, is_read presente
    assert all(isinstance(n["metadata"], dict) for n in body)
    assert all(n["is_read"] is False for n in body)
    assert all(n["user_id"] == TUTOR_A for n in body)


def test_list_only_unread_filter():
    client, db = build(current=TUTOR_A)
    _add_notification(db, title="Lida", is_read=True)
    _add_notification(db, title="NaoLida", is_read=False)
    r = client.get("/notifications", params={"only_unread": True})
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["title"] == "NaoLida"


def test_unread_count():
    client, db = build(current=TUTOR_A)
    _add_notification(db, is_read=False)
    _add_notification(db, is_read=False)
    _add_notification(db, is_read=True)
    r = client.get("/notifications/unread-count")
    assert r.status_code == 200
    assert r.json() == {"count": 2}


# ----- marcar como lida -----
def test_mark_as_read_happy_path():
    client, db = build(current=TUTOR_A)
    n = _add_notification(db, notif_id="n-read", is_read=False)
    r = client.patch(f"/notifications/{n.id}/read")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["is_read"] is True
    assert body["read_at"] is not None
    # unread-count cai para 0
    assert client.get("/notifications/unread-count").json()["count"] == 0


def test_mark_as_read_not_found():
    client, _ = build(current=TUTOR_A)
    r = client.patch("/notifications/inexistente/read")
    assert r.status_code == 404


def test_mark_all_as_read():
    client, db = build(current=TUTOR_A)
    _add_notification(db, is_read=False)
    _add_notification(db, is_read=False)
    r = client.patch("/notifications/read-all")
    assert r.status_code == 200
    assert r.json() == {"updated": 2}
    assert client.get("/notifications/unread-count").json()["count"] == 0


# ----- 401 sem auth (usa get_current_user real) -----
def test_list_requires_auth_401():
    client, _ = build(with_real_auth=True)
    r = client.get("/notifications")
    assert r.status_code == 401


def test_mark_as_read_requires_auth_401():
    client, _ = build(with_real_auth=True)
    r = client.patch("/notifications/qualquer/read")
    assert r.status_code == 401


# ----- 403 -----
def test_create_notification_forbidden_for_non_admin():
    client, _ = build(current=TUTOR_A)
    r = client.post(
        "/notifications",
        json={"title": "T", "message": "M", "user_id": TUTOR_A},
    )
    assert r.status_code == 403


def test_create_notification_allowed_for_admin():
    client, db = build(current=ADMIN_ID)
    r = client.post(
        "/notifications",
        json={"title": "Admin diz", "message": "M", "user_id": ADMIN_ID, "user_role": "admin"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["title"] == "Admin diz"


def test_mark_other_users_notification_forbidden():
    # notificacao pertence ao TUTOR_B; TUTOR_A tenta marcar como lida
    client, db = build(current=TUTOR_A)
    n = _add_notification(db, notif_id="n-de-b", user_id=TUTOR_B)
    r = client.patch(f"/notifications/{n.id}/read")
    assert r.status_code == 403


# ----- isolamento por usuario -----
def test_user_isolation_on_list():
    client, db = build(current=TUTOR_A)
    _add_notification(db, title="Da A", user_id=TUTOR_A)
    _add_notification(db, title="Da B", user_id=TUTOR_B)
    body = client.get("/notifications").json()
    assert len(body) == 1
    assert body[0]["title"] == "Da A"


def test_user_isolation_on_unread_count():
    client, db = build(current=TUTOR_A)
    _add_notification(db, user_id=TUTOR_A, is_read=False)
    _add_notification(db, user_id=TUTOR_B, is_read=False)
    _add_notification(db, user_id=TUTOR_B, is_read=False)
    assert client.get("/notifications/unread-count").json()["count"] == 1


# ----- isolamento por tenant -----
def test_tenant_isolation_hides_other_tenant_notifications():
    client, db = build(current=TUTOR_A)
    # notificacao em outro tenant, mesmo user_id -> nao deve aparecer
    _add_notification(db, title="Outro tenant", user_id=TUTOR_A, tenant_id=OTHER_TENANT_ID)
    _add_notification(db, title="Meu tenant", user_id=TUTOR_A, tenant_id=TENANT_ID)
    body = client.get("/notifications").json()
    titles = [n["title"] for n in body]
    assert "Meu tenant" in titles
    assert "Outro tenant" not in titles


def test_null_tenant_notifications_are_visible():
    # tenant_id None (broadcast) deve ser visivel para o usuario dono
    client, db = build(current=TUTOR_A)
    _add_notification(db, title="Broadcast", user_id=TUTOR_A, tenant_id=None)
    body = client.get("/notifications").json()
    assert any(n["title"] == "Broadcast" for n in body)


# ----- regressão BUG 2 — cross-tenant leak via user.tenant_id -----

def _build_multitenant(*, active_tenant_id: str):
    """Constrói app com get_db que injeta rls_tenant = active_tenant_id, simulando
    o comportamento real do TenantResolverMiddleware + get_db no FastAPI.

    Cenário BUG 2: usuário nascido em TENANT_ID mas operando em OTHER_TENANT_ID.
    _current_tenant_id deve usar o tenant ATIVO (db.info["rls_tenant"]) e NÃO
    user.tenant_id (tenant de nascimento).
    """
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    db.info["rls_tenant"] = active_tenant_id  # simula o que get_db(request) faz

    db.add(Tenant(id=TENANT_ID, name="Aumigao", slug=DEFAULT_TENANT_SLUG, status="active", plan="business"))
    db.add(Tenant(id=OTHER_TENANT_ID, name="Outro", slug="outro", status="active", plan="business"))
    # Usuário nasce em TENANT_ID mas vai operar em OTHER_TENANT_ID neste build
    db.add(User(id=TUTOR_A, email="a@test.com", password_hash="x", role="cliente", tenant_id=TENANT_ID))
    db.commit()

    test_app = FastAPI()
    test_app.include_router(notifications.router)
    test_app.dependency_overrides[get_db] = lambda: db
    test_app.dependency_overrides[get_current_user] = lambda: db.get(User, TUTOR_A)
    return TestClient(test_app), db


def test_multitenant_user_sees_only_active_tenant_notifications():
    """Regressão BUG 2: tutor nascido em TENANT_ID operando em OTHER_TENANT_ID deve
    ver SOMENTE as notificações do tenant ATIVO — não as do tenant de nascimento.

    Com o bug presente (_current_tenant_id retornava user.tenant_id), notificações de
    TENANT_ID (tenant de nascimento) vazavam para o contexto de OTHER_TENANT_ID.
    """
    client, db = _build_multitenant(active_tenant_id=OTHER_TENANT_ID)

    # Notificação do tenant de NASCIMENTO (deve ficar invisível no contexto OTHER_TENANT)
    _add_notification(db, title="Do nascimento", user_id=TUTOR_A, tenant_id=TENANT_ID)
    # Notificação do tenant ATIVO (deve aparecer)
    _add_notification(db, title="Do ativo", user_id=TUTOR_A, tenant_id=OTHER_TENANT_ID)

    body = client.get("/notifications").json()
    titles = [n["title"] for n in body]
    assert "Do ativo" in titles, "Notificação do tenant ativo deve aparecer"
    assert "Do nascimento" not in titles, (
        "BUG 2 detectado: notificação do tenant de nascimento vazando para o "
        "contexto do tenant ativo (OTHER_TENANT_ID)"
    )


def test_multitenant_unread_count_uses_active_tenant():
    """Regressão BUG 2 (unread-count): contagem deve usar o tenant ATIVO, não o de nascimento."""
    client, db = _build_multitenant(active_tenant_id=OTHER_TENANT_ID)

    # 3 não lidas no tenant de nascimento — não devem contar
    for i in range(3):
        _add_notification(db, user_id=TUTOR_A, tenant_id=TENANT_ID, is_read=False)
    # 1 não lida no tenant ativo — deve contar
    _add_notification(db, user_id=TUTOR_A, tenant_id=OTHER_TENANT_ID, is_read=False)

    r = client.get("/notifications/unread-count")
    assert r.status_code == 200
    assert r.json()["count"] == 1, (
        "BUG 2 detectado: contagem incluiu notificações do tenant de nascimento"
    )
