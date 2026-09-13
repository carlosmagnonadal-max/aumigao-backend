"""LGPD (art. 18) — GET /api/v1/me/data-export e POST /api/v1/me/account-deletion.

Padrão do projeto (ver test_auth_logout.py): FastAPI mínimo, SQLite em memória com
StaticPool, override de get_db/get_global_db. get_current_user REAL permanece ativo
para exercitar a revogação de sessão end-to-end.
"""
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 — registra todas as tabelas no Base.metadata
from app.core.database import Base, get_db, get_global_db
from app.core.security import create_access_token, get_password_hash
from app.models.audit_log import AuditLog
from app.models.legal_acceptance import LegalAcceptance
from app.models.notification import Notification
from app.models.payment import Payment
from app.models.pet import Pet
from app.models.protected_chat_message import ProtectedChatMessage
from app.models.push_token import PushToken
from app.models.support_ticket import SupportTicket
from app.models.tenant import Tenant
from app.models.tutor_profile import TutorProfile
from app.models.user import User
from app.models.walk import Walk
from app.models.walk_review import WalkReview
from app.models.walker_earning import WalkerEarning
from app.models.walker_profile import WalkerProfile
from app.routes import auth, data_rights
from app.services.data_subject_rights_service import ANONYMIZED_EMAIL_DOMAIN

PASSWORD = "SenhaForte#2026"
TENANT_A = "t-lgpd-a"
TENANT_B = "t-lgpd-b"


def _build():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    test_app = FastAPI()
    test_app.include_router(auth.router)
    test_app.include_router(data_rights.router, prefix="/api/v1")

    def _override():
        d = Session()
        try:
            yield d
        finally:
            d.close()

    test_app.dependency_overrides[get_db] = _override
    test_app.dependency_overrides[get_global_db] = _override
    client = TestClient(test_app)
    client.Session = Session
    return client


def _token(user_id: str, ver: int = 0) -> dict:
    return {"Authorization": f"Bearer {create_access_token(user_id, {'role': 'tutor', 'ver': ver})}"}


def _seed_tutor(db, *, uid, tenant_id, email, name, cpf, street, pet_name, comment, message,
                walk_status="ride_completed", password_hash=None, apple_sub=None, walker_id=None):
    db.add(User(
        id=uid, email=email, full_name=name, role="tutor", tenant_id=tenant_id, is_active=True,
        password_hash=password_hash if password_hash is not None else get_password_hash(PASSWORD),
        token_version=0, apple_sub=apple_sub,
    ))
    db.add(TutorProfile(
        id=f"tp-{uid}", user_id=uid, tenant_id=tenant_id, full_name=name, cpf=cpf,
        phone="71999990000", street=street, city="Salvador", state="BA",
        photo_url=f"/uploads/pets/{uid}-profile.jpg",
    ))
    pet_id = f"pet-{uid}"
    db.add(Pet(id=pet_id, tutor_id=uid, tenant_id=tenant_id, name=pet_name, breed="SRD",
               medications="Remedio-" + uid, emergency_contact="Vizinho 71988887777"))
    walk_id = f"walk-{uid}"
    db.add(Walk(
        id=walk_id, tutor_id=uid, tenant_id=tenant_id, walker_id=walker_id, pet_id=pet_id,
        scheduled_date="2026-09-01T10:00:00", duration_minutes=30, price=32.9,
        status="Finalizado" if walk_status == "ride_completed" else "Agendado",
        operational_status=walk_status, address_snapshot=street + ", 100", notes="Portao azul " + uid,
        security_code="4321",
    ))
    db.add(Payment(id=f"pay-{uid}", tenant_id=tenant_id, tutor_id=uid, walk_id=walk_id, amount=32.9,
                   status="paid", provider="asaas", provider_payment_id=f"pay_asaas_secret_{uid}",
                   invoice_url=f"https://asaas.example/i/{uid}"))
    if walker_id:
        db.add(WalkReview(id=f"rev-{uid}", tenant_id=tenant_id, walk_id=walk_id, tutor_id=uid,
                          walker_id=walker_id, rating=5, comment=comment))
    db.add(ProtectedChatMessage(id=f"msg-{uid}", walk_id=walk_id, sender_user_id=uid, sender_role="tutor",
                                body=message, tenant_id=tenant_id))
    db.add(LegalAcceptance(id=f"la-{uid}", user_id=uid, user_role="tutor", tenant_id=None,
                           terms_version="terms-2026-07", privacy_version="priv-2026-07"))
    db.add(PushToken(id=f"push-{uid}", user_id=uid, expo_push_token=f"ExponentPushToken[{uid}]", platform="ios"))
    db.add(Notification(id=f"n-{uid}", tenant_id=tenant_id, user_id=uid, title="Oi", message="Passeio concluido"))
    db.add(SupportTicket(tenant_id=tenant_id, user_id=uid, subject="Ajuda " + uid, description="Detalhe " + uid,
                         requester_name=name, requester_email=email))


def _seed_world(client, **tutor_kwargs):
    db = client.Session()
    db.add(Tenant(id=TENANT_A, name="Aumigao Salvador", slug="lgpd-a", status="active", plan="business"))
    db.add(Tenant(id=TENANT_B, name="PetShop Outro", slug="lgpd-b", status="active", plan="business"))
    db.add(User(id="walker-1", email="walker1@test.com", full_name="Joao Passeador Silva", role="walker",
                tenant_id=TENANT_A, is_active=True, password_hash=get_password_hash(PASSWORD)))
    db.add(WalkerProfile(id="wp-1", user_id="walker-1", full_name="Joao Passeador Silva", cpf="52998224725",
                         phone="71977776666", status="active", pix_key="walker-pix-key@test.com"))
    defaults = dict(
        uid="tutor-a", tenant_id=TENANT_A, email="ana@test.com", name="Ana Titular Souza",
        cpf="11144477735", street="Rua Titular A", pet_name="Rex-A", comment="Comentario-A otimo",
        message="Mensagem-A do tutor", walker_id="walker-1",
    )
    defaults.update(tutor_kwargs)
    _seed_tutor(db, **defaults)
    # Outro usuário, OUTRO tenant — nada dele pode aparecer na exportação de A.
    _seed_tutor(
        db, uid="tutor-b", tenant_id=TENANT_B, email="bruno@test.com", name="Bruno Terceiro Lima",
        cpf="39053344705", street="Rua Terceiro B", pet_name="Toby-B", comment="Comentario-B ruim",
        message="Mensagem-B secreta", walker_id="walker-1",
    )
    db.commit()
    db.close()


# ─────────────────────────────────────────────────────────────────────────────
# Exportação
# ─────────────────────────────────────────────────────────────────────────────

def test_export_contains_own_data_and_nothing_from_other_user_or_tenant():
    client = _build()
    _seed_world(client)

    r = client.get("/api/v1/me/data-export", headers=_token("tutor-a"))
    assert r.status_code == 200, r.text
    assert r.headers.get("cache-control") == "no-store"
    data = r.json()
    raw = json.dumps(data, ensure_ascii=False)

    assert data["account"]["email"] == "ana@test.com"
    assert data["tutor_profile"]["cpf"] == "11144477735"
    assert data["tutor_profile"]["street"] == "Rua Titular A"
    assert data["pets"][0]["name"] == "Rex-A"
    assert data["walks_as_tutor"][0]["address_snapshot"].startswith("Rua Titular A")
    assert data["walks_as_tutor"][0]["walker_first_name"] == "Joao"
    assert data["walks_as_tutor"][0]["establishment"] == "Aumigao Salvador"
    assert data["payments"][0]["amount"] == 32.9
    assert data["reviews_given"][0]["comment"] == "Comentario-A otimo"
    assert data["chat_messages_sent"][0]["body"] == "Mensagem-A do tutor"
    assert data["consents"]["legal_acceptances"][0]["privacy_version"] == "priv-2026-07"
    assert data["devices"] == [{"platform": "ios", "updated_at": data["devices"][0]["updated_at"]}]

    # Nada do outro titular (outro tenant).
    for marker in ("bruno@test.com", "Bruno", "39053344705", "Rua Terceiro B", "Toby-B",
                   "Comentario-B", "Mensagem-B", "tutor-b", "PetShop Outro"):
        assert marker not in raw, f"vazou dado de terceiro: {marker}"
    # Terceiro (passeador): só o primeiro nome — sem sobrenome, CPF, telefone, pix.
    for marker in ("Passeador Silva", "52998224725", "71977776666", "walker-pix-key", "walker-1"):
        assert marker not in raw, f"vazou dado do passeador: {marker}"


def test_export_contains_no_secrets():
    client = _build()
    _seed_world(client, apple_sub="apple-sub-secreto-123")
    db = client.Session()
    pw_hash = db.get(User, "tutor-a").password_hash
    db.close()

    r = client.get("/api/v1/me/data-export", headers=_token("tutor-a"))
    assert r.status_code == 200, r.text
    raw = r.text
    for secret in (pw_hash, "password_hash", "token_version", "apple-sub-secreto-123", "cpf_bidx",
                   "ExponentPushToken", "security_code", "pay_asaas_secret", "provider_payment_id",
                   "invoice_url", "asaas_wallet_id", "internal_notes"):
        assert secret not in raw, f"segredo exposto na exportação: {secret}"
    assert r.json()["account"]["apple_linked"] is True


def test_export_requires_auth_and_is_rate_limited():
    client = _build()
    assert client.get("/api/v1/me/data-export").status_code == 401

    _seed_world(client, uid=f"tutor-rl-{uuid4().hex[:6]}")
    db = client.Session()
    uid = db.query(User).filter(User.email == "ana@test.com").first().id
    db.close()
    for _ in range(5):
        assert client.get("/api/v1/me/data-export", headers=_token(uid)).status_code == 200
    assert client.get("/api/v1/me/data-export", headers=_token(uid)).status_code == 429


def test_export_writes_audit_log_without_pii():
    client = _build()
    _seed_world(client)
    assert client.get("/api/v1/me/data-export", headers=_token("tutor-a")).status_code == 200
    db = client.Session()
    logs = db.query(AuditLog).filter(AuditLog.action == "lgpd.data_export").all()
    assert len(logs) == 1
    assert logs[0].entity_id == "tutor-a"
    assert logs[0].ip_address is None and logs[0].user_agent is None
    db.close()


# ─────────────────────────────────────────────────────────────────────────────
# Exclusão / anonimização
# ─────────────────────────────────────────────────────────────────────────────

def test_deletion_requires_confirmation_and_reauthentication():
    client = _build()
    _seed_world(client)
    h = _token("tutor-a")

    assert client.post("/api/v1/me/account-deletion", json={"password": PASSWORD}, headers=h).status_code == 400
    r = client.post("/api/v1/me/account-deletion", json={"confirm": True}, headers=h)
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "reautenticacao_obrigatoria"
    r = client.post("/api/v1/me/account-deletion", json={"confirm": True, "password": "errada"}, headers=h)
    assert r.status_code == 403
    assert client.post("/api/v1/me/account-deletion", json={"confirm": True, "password": PASSWORD}).status_code == 401

    db = client.Session()
    assert db.get(User, "tutor-a").is_active is True  # nada mudou
    db.close()


def test_deletion_anonymizes_revokes_session_and_preserves_financial_records():
    client = _build()
    _seed_world(client)
    login = client.post("/auth/login", json={"email": "ana@test.com", "password": PASSWORD})
    assert login.status_code == 200, login.text
    h = {"Authorization": f"Bearer {login.json()['access_token']}"}
    assert client.get("/auth/me", headers=h).status_code == 200

    r = client.post("/api/v1/me/account-deletion", json={"confirm": True, "password": PASSWORD}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "deleted"

    # Sessão antiga revogada; login antigo impossível.
    assert client.get("/auth/me", headers=h).status_code == 401
    assert client.get("/api/v1/me/data-export", headers=h).status_code == 401
    assert client.post("/auth/login", json={"email": "ana@test.com", "password": PASSWORD}).status_code == 401

    db = client.Session()
    user = db.get(User, "tutor-a")
    assert user.is_active is False
    assert user.email.endswith("@" + ANONYMIZED_EMAIL_DOMAIN) and "ana" not in user.email
    assert user.full_name == "Conta excluída" and user.password_hash == "" and user.apple_sub is None

    tp = db.query(TutorProfile).filter(TutorProfile.user_id == "tutor-a").one()
    assert (tp.full_name, tp.cpf, tp.phone, tp.street, tp.city) == ("", "", "", "", "")
    assert tp.cpf_bidx is None and tp.photo_url is None

    pet = db.get(Pet, "pet-tutor-a")
    assert pet.name == "Pet removido" and pet.emergency_contact is None and pet.medications == ""

    walk = db.get(Walk, "walk-tutor-a")
    assert walk.address_snapshot == "" and walk.notes == "" and walk.security_code is None
    assert walk.price == 32.9 and walk.operational_status == "ride_completed"  # registro preservado

    pay = db.get(Payment, "pay-tutor-a")
    assert pay is not None and pay.amount == 32.9 and pay.status == "paid" and pay.tutor_id == "tutor-a"

    rev = db.get(WalkReview, "rev-tutor-a")
    assert rev.rating == 5 and rev.comment is None
    assert db.get(ProtectedChatMessage, "msg-tutor-a").body != "Mensagem-A do tutor"
    assert db.query(PushToken).filter(PushToken.user_id == "tutor-a").count() == 0
    assert db.query(Notification).filter(Notification.user_id == "tutor-a").count() == 0
    ticket = db.query(SupportTicket).filter(SupportTicket.user_id == "tutor-a").one()
    assert ticket.requester_email is None and ticket.requester_name is None
    assert db.query(LegalAcceptance).filter(LegalAcceptance.user_id == "tutor-a").count() == 1  # prova do aceite

    # Outro titular intacto.
    other = db.get(User, "tutor-b")
    assert other.is_active is True and other.email == "bruno@test.com"
    assert db.get(ProtectedChatMessage, "msg-tutor-b").body == "Mensagem-B secreta"

    # Auditoria sem PII.
    logs = db.query(AuditLog).filter(AuditLog.action == "lgpd.account_deletion").all()
    assert len(logs) == 1
    blob = json.dumps([logs[0].before_data, logs[0].after_data, logs[0].ip_address, logs[0].user_agent])
    for pii in ("ana@test.com", "Ana", "11144477735", "Rua Titular A", "Rex-A"):
        assert pii not in blob
    db.close()


def test_deletion_blocked_with_active_walk_returns_409_and_changes_nothing():
    client = _build()
    _seed_world(client, walk_status="ride_in_progress")
    r = client.post("/api/v1/me/account-deletion", json={"confirm": True, "password": PASSWORD},
                    headers=_token("tutor-a"))
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "passeio_ativo"
    assert any(x["code"] == "passeio_ativo" for x in detail["reasons"])

    db = client.Session()
    user = db.get(User, "tutor-a")
    assert user.is_active is True and user.email == "ana@test.com" and user.token_version == 0
    assert db.query(TutorProfile).filter(TutorProfile.user_id == "tutor-a").one().cpf == "11144477735"
    assert db.query(AuditLog).filter(AuditLog.action == "lgpd.account_deletion_blocked").count() == 1
    db.close()


def test_deletion_blocked_for_walker_with_pending_withdrawal_or_unreleased_earnings():
    client = _build()
    _seed_world(client)
    db = client.Session()
    db.add(Payment(id="wd-1", tutor_id="walker-1", walk_id=None, amount=-50.0, status="pending", provider="pix"))
    db.add(WalkerEarning(id="we-1", walker_id="walker-1", tenant_id=TENANT_A, walk_id="walk-x", gross=40.0,
                         platform_amount=8.0, amount=32.0, payable_at=datetime.now(timezone.utc) + timedelta(days=5)))
    db.commit()
    db.close()

    r = client.post("/api/v1/me/account-deletion", json={"confirm": True, "password": PASSWORD},
                    headers=_token("walker-1"))
    assert r.status_code == 409, r.text
    codes = {x["code"] for x in r.json()["detail"]["reasons"]}
    assert {"saque_pendente", "saldo_a_liberar"} <= codes

    db = client.Session()
    assert db.get(WalkerProfile, "wp-1").pix_key == "walker-pix-key@test.com"
    db.close()


def test_deletion_is_idempotent():
    client = _build()
    _seed_world(client)
    h = _token("tutor-a")
    body = {"confirm": True, "password": PASSWORD}
    assert client.post("/api/v1/me/account-deletion", json=body, headers=h).status_code == 200
    # Repetição com o mesmo token: sessão revogada → 401 (nunca 500 / nunca reprocessa).
    assert client.post("/api/v1/me/account-deletion", json=body, headers=h).status_code == 401

    # Pedido concorrente que já tinha passado da autenticação quando a 1ª exclusão commitou.
    from app.dependencies.auth import get_current_user

    db = client.Session()
    anonymized = db.get(User, "tutor-a")
    db.expunge(anonymized)
    db.close()
    client.app.dependency_overrides[get_current_user] = lambda: anonymized
    r = client.post("/api/v1/me/account-deletion", json=body)
    assert r.status_code == 200 and r.json()["status"] == "already_deleted"
    client.app.dependency_overrides.pop(get_current_user)

    db = client.Session()
    assert db.query(AuditLog).filter(AuditLog.action == "lgpd.account_deletion").count() == 1
    db.close()


def test_social_account_reauth_with_apple_token(monkeypatch):
    client = _build()
    _seed_world(client, password_hash="", apple_sub="apple-sub-ana")
    h = _token("tutor-a")

    monkeypatch.setattr(auth, "_decode_apple_jwt_payload", lambda token: {"sub": "apple-sub-outra-pessoa"})
    r = client.post("/api/v1/me/account-deletion", json={"confirm": True, "provider": "apple", "token": "x"}, headers=h)
    assert r.status_code == 403

    # Conta social sem senha: senha qualquer não serve.
    r = client.post("/api/v1/me/account-deletion", json={"confirm": True, "password": ""}, headers=h)
    assert r.status_code == 400 and r.json()["detail"]["method"] == "social"

    monkeypatch.setattr(auth, "_decode_apple_jwt_payload", lambda token: {"sub": "apple-sub-ana"})
    r = client.post("/api/v1/me/account-deletion", json={"confirm": True, "provider": "apple", "token": "x"}, headers=h)
    assert r.status_code == 200, r.text


def test_social_account_reauth_with_google_token(monkeypatch):
    client = _build()
    _seed_world(client, password_hash="")
    h = _token("tutor-a")

    async def _fake_google(token):
        return {"email": "ANA@test.com" if token == "ok" else "outra@test.com"}

    monkeypatch.setattr(auth, "_google_user_info", _fake_google)
    r = client.post("/api/v1/me/account-deletion", json={"confirm": True, "provider": "google", "token": "bad"}, headers=h)
    assert r.status_code == 403
    r = client.post("/api/v1/me/account-deletion", json={"confirm": True, "provider": "google", "token": "ok"}, headers=h)
    assert r.status_code == 200, r.text


def test_admin_account_cannot_self_delete():
    client = _build()
    _seed_world(client)
    db = client.Session()
    db.get(User, "tutor-a").role = "admin"
    db.commit()
    db.close()
    r = client.post("/api/v1/me/account-deletion", json={"confirm": True, "password": PASSWORD},
                    headers=_token("tutor-a"))
    assert r.status_code == 409 and r.json()["detail"]["code"] == "conta_administrativa"
