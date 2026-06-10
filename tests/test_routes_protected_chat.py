"""Testes de ROTA (camada HTTP) do modulo app/routes/protected_chat.py.

Padrao do projeto (ver tests/test_routes_onda1.py e tests/test_routes_auth.py):
monta um FastAPI MINIMO so com o router de protected_chat, SQLite em memoria
(StaticPool), overrides de get_db / get_current_user. NAO importa app.main
(que conecta no banco de PROD).

O "protected chat" e um chat tutor<->passeador vinculados a um passeio, liberado
apenas durante a janela operacional (30 min antes ate 30 min depois da conclusao),
com status operacional permitido e passeador ja aceito. Cobre: enviar/listar
mensagens (happy path), 401 (sem auth), 403 (gating: nao participante, sem walker,
status nao permitido, cancelado, janela), 404 (passeio inexistente), marcacao de
read_at ao listar e notificacao gerada ao enviar.
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
from app.models.notification import Notification
from app.models.pet import Pet
from app.models.protected_chat_message import ProtectedChatMessage
from app.models.tenant import Tenant
from app.models.user import User
from app.models.walk import Walk
from app.routes import protected_chat
from app.services.tenant_seed_service import DEFAULT_TENANT_SLUG

TENANT_ID = "t-test"
TUTOR_ID = "tutor-test"
WALKER_ID = "walker-test"
OUTSIDER_ID = "outsider-test"
WALK_ID = "walk-1"
PET_ID = "pet-1"


def _scheduled_now_iso() -> str:
    # Horario do passeio = agora (janela aberta: 30 min antes ate em progresso).
    return datetime.utcnow().replace(microsecond=0).isoformat()


def build(*, walk_overrides: dict | None = None, create_walk: bool = True):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    db.add(Tenant(id=TENANT_ID, name="Aumigao", slug=DEFAULT_TENANT_SLUG, status="active", plan="business"))
    db.add(User(id=TUTOR_ID, email="tutor@test.com", password_hash="x", role="cliente", tenant_id=TENANT_ID))
    db.add(User(id=WALKER_ID, email="walker@test.com", password_hash="x", role="passeador", tenant_id=TENANT_ID))
    db.add(User(id=OUTSIDER_ID, email="outsider@test.com", password_hash="x", role="cliente", tenant_id=TENANT_ID))
    db.add(Pet(id=PET_ID, tutor_id=TUTOR_ID, name="Rex"))

    if create_walk:
        walk_kwargs = dict(
            id=WALK_ID,
            tutor_id=TUTOR_ID,
            tenant_id=TENANT_ID,
            walker_id=WALKER_ID,
            assigned_walker_id=WALKER_ID,
            pet_id=PET_ID,
            scheduled_date=_scheduled_now_iso(),
            duration_minutes=45,
            price=50.0,
            status="walker_accepted",
            operational_status="ride_in_progress",
        )
        walk_kwargs.update(walk_overrides or {})
        db.add(Walk(**walk_kwargs))

    db.commit()

    test_app = FastAPI()
    test_app.include_router(protected_chat.router)
    test_app.dependency_overrides[get_db] = lambda: db
    test_app.dependency_overrides[get_current_user] = lambda: db.get(User, TUTOR_ID)
    return test_app, db


def client_as(test_app, db, user_id):
    test_app.dependency_overrides[get_current_user] = lambda: db.get(User, user_id)
    return TestClient(test_app)


# ----------------------------------------------------------------- 401 -------
def test_messages_requires_auth_401():
    test_app, _ = build()
    # remove override de get_current_user -> HTTPBearer auto_error=False -> 401
    test_app.dependency_overrides.pop(get_current_user, None)
    client = TestClient(test_app)
    r = client.get("/protected-chat/messages", params={"walk_id": WALK_ID})
    assert r.status_code == 401


def test_post_message_requires_auth_401():
    test_app, _ = build()
    test_app.dependency_overrides.pop(get_current_user, None)
    client = TestClient(test_app)
    r = client.post("/protected-chat/messages", json={"walk_id": WALK_ID, "body": "ola"})
    assert r.status_code == 401


# ----------------------------------------------------------------- 404 -------
def test_messages_walk_not_found_404():
    test_app, db = build(create_walk=False)
    client = client_as(test_app, db, TUTOR_ID)
    r = client.get("/protected-chat/messages", params={"walk_id": "inexistente"})
    assert r.status_code == 404


def test_post_message_walk_not_found_404():
    test_app, db = build(create_walk=False)
    client = client_as(test_app, db, TUTOR_ID)
    r = client.post("/protected-chat/messages", json={"walk_id": "inexistente", "body": "ola"})
    assert r.status_code == 404


# ------------------------------------------------------------ happy path -----
def test_tutor_sends_message_happy_path():
    test_app, db = build()
    client = client_as(test_app, db, TUTOR_ID)
    r = client.post("/protected-chat/messages", json={"walk_id": WALK_ID, "body": "Oi, ja saiu?"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["walk_id"] == WALK_ID
    assert body["sender_user_id"] == TUTOR_ID
    assert body["sender_role"] == "tutor"
    assert body["body"] == "Oi, ja saiu?"
    assert body["read_at"] is None
    # persistido
    assert db.query(ProtectedChatMessage).count() == 1


def test_walker_sends_message_role_walker():
    test_app, db = build()
    client = client_as(test_app, db, WALKER_ID)
    r = client.post("/protected-chat/messages", json={"walk_id": WALK_ID, "body": "Estou a caminho"})
    assert r.status_code == 200, r.text
    assert r.json()["sender_role"] == "walker"


def test_send_message_creates_notification_for_other_participant():
    test_app, db = build()
    client = client_as(test_app, db, TUTOR_ID)
    client.post("/protected-chat/messages", json={"walk_id": WALK_ID, "body": "ola"})
    notif = db.query(Notification).filter(Notification.type == "protected_chat_message").first()
    assert notif is not None
    assert notif.user_id == WALKER_ID  # destinatario = o outro participante
    assert notif.user_role == "walker"
    assert notif.related_entity_id == WALK_ID


def test_list_messages_ordered_and_marks_read():
    test_app, db = build()
    # tutor envia 1 msg; walker envia 1 msg
    tutor_client = client_as(test_app, db, TUTOR_ID)
    tutor_client.post("/protected-chat/messages", json={"walk_id": WALK_ID, "body": "msg do tutor"})
    walker_client = client_as(test_app, db, WALKER_ID)
    walker_client.post("/protected-chat/messages", json={"walk_id": WALK_ID, "body": "msg do walker"})

    # tutor lista: deve marcar como lida a mensagem do walker (do outro), nao a sua
    r = client_as(test_app, db, TUTOR_ID).get("/protected-chat/messages", params={"walk_id": WALK_ID})
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["chat_available"] is True
    items = payload["items"]
    assert len(items) == 2
    assert [m["body"] for m in items] == ["msg do tutor", "msg do walker"]  # ordem asc por created_at
    by_role = {m["sender_role"]: m for m in items}
    assert by_role["walker"]["read_at"] is not None  # mensagem do outro -> marcada lida
    assert by_role["tutor"]["read_at"] is None  # propria mensagem -> nao marcada


# ----------------------------------------------------------------- 403 -------
def test_outsider_cannot_access_chat_403():
    test_app, db = build()
    client = client_as(test_app, db, OUTSIDER_ID)
    r = client.get("/protected-chat/messages", params={"walk_id": WALK_ID})
    assert r.status_code == 403


def test_chat_blocked_when_no_walker_assigned_403():
    test_app, db = build(walk_overrides={"walker_id": None, "assigned_walker_id": None})
    client = client_as(test_app, db, TUTOR_ID)
    r = client.get("/protected-chat/messages", params={"walk_id": WALK_ID})
    assert r.status_code == 403
    assert "passeador" in r.json()["detail"].lower()


def test_chat_blocked_when_cancelled_403():
    test_app, db = build(walk_overrides={"operational_status": "ride_cancelled", "status": "cancelled"})
    client = client_as(test_app, db, TUTOR_ID)
    r = client.get("/protected-chat/messages", params={"walk_id": WALK_ID})
    assert r.status_code == 403
    assert "cancel" in r.json()["detail"].lower()


def test_chat_blocked_when_status_not_allowed_403():
    test_app, db = build(walk_overrides={"operational_status": "pending", "status": "pending"})
    client = client_as(test_app, db, TUTOR_ID)
    r = client.get("/protected-chat/messages", params={"walk_id": WALK_ID})
    assert r.status_code == 403
    assert "janela" in r.json()["detail"].lower()


def test_chat_blocked_too_early_before_window_403():
    # passeio agendado para daqui a 2h -> antes da abertura (30 min antes)
    future = (datetime.utcnow() + timedelta(hours=2)).replace(microsecond=0).isoformat()
    test_app, db = build(walk_overrides={"scheduled_date": future})
    client = client_as(test_app, db, TUTOR_ID)
    r = client.get("/protected-chat/messages", params={"walk_id": WALK_ID})
    assert r.status_code == 403
    assert "30 minutos" in r.json()["detail"]


def test_chat_blocked_after_completion_window_403():
    # ride_completed com horario muito no passado -> janela pos-conclusao fechada
    past = (datetime.utcnow() - timedelta(hours=3)).replace(microsecond=0).isoformat()
    test_app, db = build(walk_overrides={"scheduled_date": past, "operational_status": "ride_completed", "status": "ride_completed"})
    client = client_as(test_app, db, TUTOR_ID)
    r = client.get("/protected-chat/messages", params={"walk_id": WALK_ID})
    assert r.status_code == 403
    assert "encerrado" in r.json()["detail"].lower()


# ------------------------------------------------------------ validacao ------
def test_post_message_rejects_empty_body_422():
    # body com min_length=1 -> pydantic rejeita string vazia (422)
    test_app, db = build()
    client = client_as(test_app, db, TUTOR_ID)
    r = client.post("/protected-chat/messages", json={"walk_id": WALK_ID, "body": ""})
    assert r.status_code == 422


def test_post_message_rejects_too_long_body_422():
    test_app, db = build()
    client = client_as(test_app, db, TUTOR_ID)
    r = client.post("/protected-chat/messages", json={"walk_id": WALK_ID, "body": "x" * 1001})
    assert r.status_code == 422


# ------------------------------------------------------------ api_router -----
def test_api_router_path_also_works():
    # protected_chat expoe tambem api_router (prefixo /api/protected-chat)
    test_app, db = build()
    test_app.include_router(protected_chat.api_router)
    client = client_as(test_app, db, TUTOR_ID)
    r = client.post("/api/protected-chat/messages", json={"walk_id": WALK_ID, "body": "via api"})
    assert r.status_code == 200, r.text
    assert r.json()["body"] == "via api"
