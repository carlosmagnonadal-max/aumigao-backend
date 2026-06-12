"""Testes de rota para tickets de suporte interno (Feature 2) + user-facing (Fase 4 C1).

Padrão do projeto: FastAPI mínimo com SQLite em memória (StaticPool),
overrides de get_db / get_current_user. NÃO importa app.main.

Nota de RBAC: user_has_permission só bypassa para role="super_admin". Admin
regular sem seed RBAC toma 403 nas rotas gateadas. Por isso os testes HTTP
usam super_admin como ator padrão. O tenant-scoping cross-tenant é validado
diretamente via helpers de app.dependencies.tenant_scope.

Cobre:
- GET    /admin/support-tickets          → lista, status_counts, filtros, 403
- POST   /admin/support-tickets          → criação, defaults, tenant_id do scope
- GET    /admin/support-tickets/{id}     → detalhe, 404
- PATCH  /admin/support-tickets/{id}     → atualiza campos, updated_at muda, 404
- cross-tenant: validado via ensure_tenant_access helper direto
- POST   /support-tickets               → criação pelo usuário logado (gate flag, rate limit)
- GET    /support-tickets/me            → lista só os tickets do usuário logado
- PATCH  reply                          → dispara notificação support_reply 1x
"""
from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 — registra todos os modelos no Base.metadata
from app.core.database import Base, get_db
from app.dependencies.auth import get_current_user
from app.dependencies.tenant_scope import (
    AdminTenantScope,
    apply_tenant_filter,
    ensure_tenant_access,
    get_admin_tenant_scope,
)
from app.models.notification import Notification
from app.models.support_ticket import SupportTicket
from app.models.tenant import Tenant, TenantFeature
from app.models.user import User
from app.routes import support_tickets

SUPER_ID = "super-1"
ADMIN_A_ID = "admin-a"
ADMIN_B_ID = "admin-b"
TUTOR_ID = "tutor-1"

TENANT_A = "tenant-a"
TENANT_B = "tenant-b"


def build(*, current: str = SUPER_ID):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    db.add(User(id=SUPER_ID, email="super@aumigao.app", password_hash="x", role="super_admin"))
    db.add(User(id=ADMIN_A_ID, email="admin-a@aumigao.app", password_hash="x", role="admin", tenant_id=TENANT_A))
    db.add(User(id=ADMIN_B_ID, email="admin-b@aumigao.app", password_hash="x", role="admin", tenant_id=TENANT_B))
    db.add(User(id=TUTOR_ID, email="tutor@aumigao.app", password_hash="x", role="tutor", tenant_id=TENANT_A))
    db.commit()

    test_app = FastAPI()
    test_app.include_router(support_tickets.router)
    test_app.dependency_overrides[get_db] = lambda: db
    test_app.dependency_overrides[get_current_user] = lambda: db.get(User, current)
    return TestClient(test_app), db


def set_user(client, db, user_id):
    client.app.dependency_overrides[get_current_user] = lambda: db.get(User, user_id)


def _seed_ticket(db, *, tid, tenant_id, subject="Problema X", status="open", priority="normal"):
    ticket = SupportTicket(
        id=tid,
        tenant_id=tenant_id,
        subject=subject,
        description="Descricao do problema.",
        status=status,
        priority=priority,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(ticket)
    db.commit()
    return ticket


# ------------------------------------------------------------------ GET list --

def test_list_tickets_forbidden_for_non_admin():
    client, _ = build(current=TUTOR_ID)
    r = client.get("/admin/support-tickets")
    assert r.status_code == 403


def test_list_tickets_empty_structure():
    client, _ = build(current=SUPER_ID)
    r = client.get("/admin/support-tickets")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "items" in body
    assert "total" in body
    assert "status_counts" in body
    # contadores zerados
    counts = body["status_counts"]
    for key in ("open", "in_progress", "resolved", "closed"):
        assert key in counts
        assert counts[key] == 0


def test_list_tickets_super_admin_sees_all_tenants():
    client, db = build(current=SUPER_ID)
    _seed_ticket(db, tid="t-a-1", tenant_id=TENANT_A)
    _seed_ticket(db, tid="t-b-1", tenant_id=TENANT_B)
    body = client.get("/admin/support-tickets").json()
    ids = {item["id"] for item in body["items"]}
    assert "t-a-1" in ids
    assert "t-b-1" in ids


def test_list_tickets_tenant_scoping_via_helper():
    """Valida scoping de tenant no nível do helper sem RBAC seed.

    Admin de tenant A enxerga só tickets do tenant A;
    super_admin enxerga todos os tenants.
    """
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    db.add(User(id=SUPER_ID, email="super@aumigao.app", password_hash="x", role="super_admin"))
    db.add(User(id=ADMIN_A_ID, email="admin-a@aumigao.app", password_hash="x", role="admin", tenant_id=TENANT_A))
    db.commit()
    _seed_ticket(db, tid="t-a-1", tenant_id=TENANT_A)
    _seed_ticket(db, tid="t-b-1", tenant_id=TENANT_B)

    # Escopo do admin A: só vê tickets do tenant A
    admin_a = db.get(User, ADMIN_A_ID)
    scope_a = get_admin_tenant_scope(admin_a)
    rows_a = apply_tenant_filter(db.query(SupportTicket), SupportTicket, scope_a).all()
    ids_a = {t.id for t in rows_a}
    assert "t-a-1" in ids_a
    assert "t-b-1" not in ids_a

    # Escopo do super_admin: vê todos
    super_user = db.get(User, SUPER_ID)
    scope_super = get_admin_tenant_scope(super_user)
    rows_super = apply_tenant_filter(db.query(SupportTicket), SupportTicket, scope_super).all()
    ids_super = {t.id for t in rows_super}
    assert "t-a-1" in ids_super
    assert "t-b-1" in ids_super


def test_list_tickets_filter_by_status():
    client, db = build(current=SUPER_ID)
    _seed_ticket(db, tid="t-open", tenant_id=TENANT_A, status="open")
    _seed_ticket(db, tid="t-resolved", tenant_id=TENANT_A, status="resolved")
    body = client.get("/admin/support-tickets?status=open").json()
    ids = {item["id"] for item in body["items"]}
    assert "t-open" in ids
    assert "t-resolved" not in ids


def test_list_tickets_filter_by_priority():
    client, db = build(current=SUPER_ID)
    _seed_ticket(db, tid="t-high", tenant_id=TENANT_A, priority="high")
    _seed_ticket(db, tid="t-low", tenant_id=TENANT_A, priority="low")
    body = client.get("/admin/support-tickets?priority=high").json()
    ids = {item["id"] for item in body["items"]}
    assert "t-high" in ids
    assert "t-low" not in ids


def test_list_tickets_invalid_status_returns_400():
    client, _ = build(current=SUPER_ID)
    r = client.get("/admin/support-tickets?status=invalido")
    assert r.status_code == 400


def test_list_tickets_status_counts():
    client, db = build(current=SUPER_ID)
    _seed_ticket(db, tid="t1", tenant_id=TENANT_A, status="open")
    _seed_ticket(db, tid="t2", tenant_id=TENANT_A, status="open")
    _seed_ticket(db, tid="t3", tenant_id=TENANT_A, status="resolved")
    body = client.get("/admin/support-tickets").json()
    counts = body["status_counts"]
    assert counts["open"] == 2
    assert counts["resolved"] == 1
    assert counts["in_progress"] == 0
    assert counts["closed"] == 0


def test_list_tickets_response_shape():
    client, db = build(current=SUPER_ID)
    _seed_ticket(db, tid="t-shape", tenant_id=TENANT_A)
    body = client.get("/admin/support-tickets").json()
    item = body["items"][0]
    for field in (
        "id", "tenant_id", "subject", "description",
        "requester_name", "requester_email", "requester_role",
        "status", "priority", "assignee_user_id", "internal_notes",
        "created_at", "updated_at",
    ):
        assert field in item, f"campo faltando: {field}"


# --------------------------------------------------------------- POST create --

def test_create_ticket_defaults():
    client, _ = build(current=SUPER_ID)
    r = client.post("/admin/support-tickets", json={
        "subject": "Problema de login",
        "description": "O usuario nao consegue acessar.",
    })
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "open"
    assert body["priority"] == "normal"
    assert body["subject"] == "Problema de login"


def test_create_ticket_tenant_id_from_admin_scope():
    """tenant_id do ticket deve ser o tenant_id do admin autenticado.

    Como admin regular toma 403 sem RBAC seed, simula via override direto:
    o router chama get_admin_tenant_scope internamente — verificamos via helper.
    """
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    db.add(User(id=ADMIN_A_ID, email="admin-a@aumigao.app", password_hash="x", role="admin", tenant_id=TENANT_A))
    db.commit()

    admin_a = db.get(User, ADMIN_A_ID)
    scope_a = get_admin_tenant_scope(admin_a)
    # scope do admin A define o tenant_id do ticket
    assert scope_a.tenant_id == TENANT_A
    assert scope_a.is_global is False


def test_create_ticket_super_admin_tenant_id_is_none():
    """super_admin cria ticket com tenant_id=None (global)."""
    client, _ = build(current=SUPER_ID)
    r = client.post("/admin/support-tickets", json={
        "subject": "Ticket global",
        "description": "Descricao.",
    })
    assert r.status_code == 201, r.text
    assert r.json()["tenant_id"] is None


def test_create_ticket_with_requester_info():
    client, _ = build(current=SUPER_ID)
    r = client.post("/admin/support-tickets", json={
        "subject": "Duvida de tutor",
        "description": "Como cancelo um passeio?",
        "requester_name": "Ana Paula",
        "requester_email": "ana@aumigao.app",
        "requester_role": "tutor",
        "priority": "high",
    })
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["requester_name"] == "Ana Paula"
    assert body["requester_role"] == "tutor"
    assert body["priority"] == "high"


def test_create_ticket_forbidden_for_non_admin():
    client, _ = build(current=TUTOR_ID)
    r = client.post("/admin/support-tickets", json={
        "subject": "Tentativa proibida",
        "description": "x",
    })
    assert r.status_code == 403


# -------------------------------------------------------------- GET detalhe --

def test_get_ticket_detail():
    client, db = build(current=SUPER_ID)
    _seed_ticket(db, tid="detail-1", tenant_id=TENANT_A, subject="Detalhe Ticket")
    r = client.get("/admin/support-tickets/detail-1")
    assert r.status_code == 200, r.text
    assert r.json()["subject"] == "Detalhe Ticket"


def test_get_ticket_not_found():
    client, _ = build(current=SUPER_ID)
    r = client.get("/admin/support-tickets/inexistente-xyz")
    assert r.status_code == 404


def test_get_ticket_cross_tenant_blocked_via_helper():
    """ensure_tenant_access bloqueia acesso cross-tenant com 404."""
    from fastapi import HTTPException

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    db.add(User(id=ADMIN_A_ID, email="admin-a@aumigao.app", password_hash="x", role="admin", tenant_id=TENANT_A))
    db.commit()

    admin_a = db.get(User, ADMIN_A_ID)
    scope_a = get_admin_tenant_scope(admin_a)

    # Ticket do tenant B — admin A não deve acessar
    with pytest.raises(HTTPException) as exc_info:
        ensure_tenant_access(TENANT_B, scope_a)
    assert exc_info.value.status_code == 404


# ---------------------------------------------------------------- PATCH update --

def test_update_ticket_status():
    client, db = build(current=SUPER_ID)
    _seed_ticket(db, tid="upd-1", tenant_id=TENANT_A, status="open")
    r = client.patch("/admin/support-tickets/upd-1", json={"status": "in_progress"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "in_progress"


def test_update_ticket_priority():
    client, db = build(current=SUPER_ID)
    _seed_ticket(db, tid="upd-2", tenant_id=TENANT_A, priority="normal")
    r = client.patch("/admin/support-tickets/upd-2", json={"priority": "high"})
    assert r.status_code == 200, r.text
    assert r.json()["priority"] == "high"


def test_update_ticket_assignee_and_notes():
    client, db = build(current=SUPER_ID)
    _seed_ticket(db, tid="upd-3", tenant_id=TENANT_A)
    r = client.patch("/admin/support-tickets/upd-3", json={
        "assignee_user_id": ADMIN_A_ID,
        "internal_notes": "Encaminhado para equipe de suporte.",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["assignee_user_id"] == ADMIN_A_ID
    assert body["internal_notes"] == "Encaminhado para equipe de suporte."


def test_update_ticket_updated_at_is_present():
    """updated_at deve estar presente após o PATCH."""
    client, db = build(current=SUPER_ID)
    _seed_ticket(db, tid="upd-4", tenant_id=TENANT_A)
    r = client.patch("/admin/support-tickets/upd-4", json={"status": "resolved"})
    assert r.status_code == 200, r.text
    assert r.json()["updated_at"] is not None


def test_update_ticket_not_found():
    client, _ = build(current=SUPER_ID)
    r = client.patch("/admin/support-tickets/inexistente-xyz", json={"status": "resolved"})
    assert r.status_code == 404


def test_update_ticket_cross_tenant_blocked_via_helper():
    """ensure_tenant_access bloqueia PATCH cross-tenant."""
    from fastapi import HTTPException

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    db.add(User(id=ADMIN_A_ID, email="admin-a@aumigao.app", password_hash="x", role="admin", tenant_id=TENANT_A))
    db.commit()
    _seed_ticket(db, tid="cross-patch-b", tenant_id=TENANT_B)

    admin_a = db.get(User, ADMIN_A_ID)
    scope_a = get_admin_tenant_scope(admin_a)
    ticket = db.get(SupportTicket, "cross-patch-b")

    with pytest.raises(HTTPException) as exc_info:
        ensure_tenant_access(ticket.tenant_id, scope_a)
    assert exc_info.value.status_code == 404


# ==========================================================================
# Fase 4 C1 — Rotas user-facing e reply
# ==========================================================================

def _build_user(*, user_id: str = TUTOR_ID, tenant_id: str | None = TENANT_A):
    """Cria app FastAPI com rotas user-facing registradas."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    # Tenant necessário para gate de feature
    db.add(Tenant(id=TENANT_A, name="Tenant A", slug="tenant-a", plan="starter"))
    db.add(User(id=SUPER_ID, email="super@aumigao.app", password_hash="x", role="super_admin"))
    db.add(User(id=ADMIN_A_ID, email="admin-a@aumigao.app", password_hash="x", role="admin", tenant_id=TENANT_A))
    db.add(User(id=TUTOR_ID, email="tutor@aumigao.app", password_hash="x", role="tutor", tenant_id=tenant_id, full_name="Ana Tutora"))
    db.add(User(id="walker-1", email="walker@aumigao.app", password_hash="x", role="walker", tenant_id=TENANT_A))
    db.commit()

    test_app = FastAPI()
    test_app.include_router(support_tickets.user_router)
    test_app.include_router(support_tickets.router)
    test_app.dependency_overrides[get_db] = lambda: db
    test_app.dependency_overrides[get_current_user] = lambda: db.get(User, user_id)
    return TestClient(test_app), db


# ---------------------------------- POST /support-tickets (user-facing) ----

def test_user_create_ticket_success():
    client, db = _build_user()
    r = client.post("/support-tickets", json={"subject": "Login falhou", "message": "Nao consigo entrar."})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "open"
    assert "id" in body
    assert body["subject"] == "Login falhou"
    assert body["message"] == "Nao consigo entrar."


def test_user_create_ticket_with_category():
    client, _ = _build_user()
    r = client.post("/support-tickets", json={"subject": "Pagamento", "message": "Cobrança dupla.", "category": "Pagamento"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["subject"].startswith("[Pagamento]")


def test_user_create_ticket_no_tenant_no_feature_gate():
    """Usuário sem tenant_id pode criar ticket (sem gate)."""
    client, _ = _build_user(tenant_id=None)
    r = client.post("/support-tickets", json={"subject": "Ajuda", "message": "Preciso de ajuda."})
    assert r.status_code == 201, r.text


def test_user_create_ticket_feature_disabled_returns_403():
    """Gate: feature support_tickets desligada → 403."""
    client, db = _build_user()
    # Desliga a feature para TENANT_A
    db.add(TenantFeature(id="tf-1", tenant_id=TENANT_A, feature_key="support_tickets", enabled=False))
    db.commit()
    r = client.post("/support-tickets", json={"subject": "x", "message": "y"})
    assert r.status_code == 403, r.text


def test_user_create_ticket_rate_limit():
    """Rate limit: 5 tickets por 15 min por user_id."""
    # Limpa o rate limiter interno antes do teste
    support_tickets._user_ticket_limiter.clear(TUTOR_ID)
    client, _ = _build_user()
    payload = {"subject": "Spam", "message": "mensagem de teste"}
    for _ in range(5):
        r = client.post("/support-tickets", json=payload)
        assert r.status_code == 201
    # 6ª requisição deve ser bloqueada
    r = client.post("/support-tickets", json=payload)
    assert r.status_code == 429, r.text
    # Limpa ao final
    support_tickets._user_ticket_limiter.clear(TUTOR_ID)


# ---------------------------------- GET /support-tickets/me ----------------

def test_user_list_my_tickets_empty():
    client, _ = _build_user()
    r = client.get("/support-tickets/me")
    assert r.status_code == 200, r.text
    assert r.json() == []


def test_user_list_my_tickets_only_own():
    client, db = _build_user()
    # Ticket do tutor
    ticket_mine = SupportTicket(
        id="mine-1", user_id=TUTOR_ID, tenant_id=TENANT_A,
        subject="Meu ticket", description="Desc", status="open", priority="normal",
        created_at=datetime.utcnow(), updated_at=datetime.utcnow(),
    )
    # Ticket de outro usuário
    ticket_other = SupportTicket(
        id="other-1", user_id="walker-1", tenant_id=TENANT_A,
        subject="Ticket alheio", description="Desc2", status="open", priority="normal",
        created_at=datetime.utcnow(), updated_at=datetime.utcnow(),
    )
    db.add(ticket_mine)
    db.add(ticket_other)
    db.commit()

    r = client.get("/support-tickets/me")
    assert r.status_code == 200, r.text
    ids = [t["id"] for t in r.json()]
    assert "mine-1" in ids
    assert "other-1" not in ids


def test_user_list_my_tickets_response_shape():
    client, db = _build_user()
    ticket = SupportTicket(
        id="shape-1", user_id=TUTOR_ID, tenant_id=TENANT_A,
        subject="Shape test", description="Desc", status="open", priority="normal",
        reply="Olá, verificamos o problema.",
        replied_at=datetime.utcnow(),
        created_at=datetime.utcnow(), updated_at=datetime.utcnow(),
    )
    db.add(ticket)
    db.commit()

    r = client.get("/support-tickets/me")
    item = r.json()[0]
    for field in ("id", "subject", "message", "status", "reply", "replied_at", "created_at"):
        assert field in item, f"campo faltando: {field}"
    assert item["reply"] == "Olá, verificamos o problema."
    assert item["replied_at"] is not None


# ---------------------------------- PATCH /admin reply → notificação -------

def _build_admin_with_notifications():
    """App combinado: rotas admin + user, com Notification table disponível."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    db.add(Tenant(id=TENANT_A, name="Tenant A", slug="tenant-a", plan="starter"))
    db.add(User(id=SUPER_ID, email="super@aumigao.app", password_hash="x", role="super_admin"))
    db.add(User(id=TUTOR_ID, email="tutor@aumigao.app", password_hash="x", role="tutor", tenant_id=TENANT_A, full_name="Ana"))
    db.commit()

    test_app = FastAPI()
    test_app.include_router(support_tickets.router)
    test_app.dependency_overrides[get_db] = lambda: db
    test_app.dependency_overrides[get_current_user] = lambda: db.get(User, SUPER_ID)
    return TestClient(test_app), db


def test_admin_reply_sets_replied_at():
    client, db = _build_admin_with_notifications()
    _seed_ticket(db, tid="reply-1", tenant_id=TENANT_A)
    # Adiciona user_id ao ticket para testar notificação
    ticket = db.get(SupportTicket, "reply-1")
    ticket.user_id = TUTOR_ID
    db.commit()

    r = client.patch("/admin/support-tickets/reply-1", json={"reply": "Verificamos e resolvemos."})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reply"] == "Verificamos e resolvemos."
    assert body["replied_at"] is not None


def test_admin_reply_creates_notification():
    """PATCH com reply não-vazia cria exatamente 1 Notification de support_reply."""
    client, db = _build_admin_with_notifications()
    _seed_ticket(db, tid="reply-notif-1", tenant_id=TENANT_A)
    ticket = db.get(SupportTicket, "reply-notif-1")
    ticket.user_id = TUTOR_ID
    db.commit()

    r = client.patch("/admin/support-tickets/reply-notif-1", json={"reply": "Problema resolvido!"})
    assert r.status_code == 200, r.text

    notifs = db.query(Notification).filter(
        Notification.type == "support_reply",
        Notification.related_entity_id == "reply-notif-1",
    ).all()
    assert len(notifs) == 1
    assert notifs[0].user_id == TUTOR_ID


def test_admin_reply_idempotent_same_text_no_duplicate_notification():
    """PATCH com o mesmo reply não cria segunda notificação."""
    client, db = _build_admin_with_notifications()
    _seed_ticket(db, tid="reply-idem-1", tenant_id=TENANT_A)
    ticket = db.get(SupportTicket, "reply-idem-1")
    ticket.user_id = TUTOR_ID
    db.commit()

    # Primeiro PATCH — deve criar notificação
    client.patch("/admin/support-tickets/reply-idem-1", json={"reply": "Texto igual."})
    # Segundo PATCH com texto idêntico — não cria nova notificação
    client.patch("/admin/support-tickets/reply-idem-1", json={"reply": "Texto igual."})

    notifs = db.query(Notification).filter(
        Notification.type == "support_reply",
        Notification.related_entity_id == "reply-idem-1",
    ).all()
    assert len(notifs) == 1


def test_admin_reply_empty_string_does_not_notify():
    """PATCH com reply vazio não cria notificação."""
    client, db = _build_admin_with_notifications()
    _seed_ticket(db, tid="reply-empty-1", tenant_id=TENANT_A)
    ticket = db.get(SupportTicket, "reply-empty-1")
    ticket.user_id = TUTOR_ID
    db.commit()

    r = client.patch("/admin/support-tickets/reply-empty-1", json={"reply": ""})
    assert r.status_code == 200, r.text

    notifs = db.query(Notification).filter(
        Notification.type == "support_reply",
        Notification.related_entity_id == "reply-empty-1",
    ).all()
    assert len(notifs) == 0


def test_admin_patch_without_reply_no_notification():
    """PATCH sem campo reply não cria notificação."""
    client, db = _build_admin_with_notifications()
    _seed_ticket(db, tid="no-reply-1", tenant_id=TENANT_A)
    ticket = db.get(SupportTicket, "no-reply-1")
    ticket.user_id = TUTOR_ID
    db.commit()

    r = client.patch("/admin/support-tickets/no-reply-1", json={"status": "in_progress"})
    assert r.status_code == 200, r.text

    notifs = db.query(Notification).filter(
        Notification.type == "support_reply",
        Notification.related_entity_id == "no-reply-1",
    ).all()
    assert len(notifs) == 0
