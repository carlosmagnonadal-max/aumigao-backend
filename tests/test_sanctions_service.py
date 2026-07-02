"""BG-6 — checagem automatica de sancoes (Portal da Transparencia, gratis).

Cobre os 4 comportamentos exigidos:
- SEM chave TRANSPARENCIA_API_KEY  -> NO-OP total (retorna None, nao toca no profile).
- COM chave, CEIS/CNEP vazios      -> status "clear" + timestamp.
- COM chave, ha sancao (hit)       -> status "hit" + timestamp + notifica admins do tenant.
- COM chave, timeout/erro/HTTP!=200 -> FAIL-OPEN: status "error", NUNCA levanta excecao.

O servico consulta o CPF do profile (EncryptedString devolve texto puro). Persistencia
MINIMIZADA (LGPD): guarda so o veredito + timestamp, nunca o dossie.
"""
from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import httpx
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.core.database import Base
from app.models.notification import Notification
from app.models.tenant import Tenant
from app.models.user import User
from app.models.walker_profile import WalkerProfile
from app.services.background import sanctions_service as svc

TENANT_ID = "t-sanc"


def _build():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(Tenant(id=TENANT_ID, name="Aumigao", slug="aumigao", status="active", plan="business"))
    db.add(User(id="adm-1", email="adm@correio.com", password_hash="x", role="admin", full_name="Adm", tenant_id=TENANT_ID))
    db.add(User(id="adm-2", email="sup@correio.com", password_hash="x", role="super_admin", full_name="Sup", tenant_id=TENANT_ID))
    prof = WalkerProfile(
        id="prof-1", user_id="u-1", full_name="Joao", cpf="52998224725",
        city="Sao Paulo", state="SP", status="under_review", created_at=datetime.utcnow(),
    )
    db.add(prof)
    db.commit()
    return db, prof


def _admin_notifs(db):
    return db.query(Notification).filter(Notification.type == "sanctions_hit_review").all()


# --------------------------------------------------------------- SEM chave (no-op)
def test_no_key_is_silent_noop(monkeypatch):
    monkeypatch.delenv("TRANSPARENCIA_API_KEY", raising=False)
    db, prof = _build()
    result = svc.run_sanctions_check(db, prof, TENANT_ID)
    assert result is None
    assert prof.sanctions_check_status == "none"
    assert prof.sanctions_checked_at is None
    assert _admin_notifs(db) == []


# ---------------------------------------------------------------- COM chave: clear
def test_key_present_clear(monkeypatch):
    monkeypatch.setenv("TRANSPARENCIA_API_KEY", "chave-teste")
    db, prof = _build()

    def _fake_get(url, headers=None, params=None, timeout=None):  # noqa: ARG001
        return httpx.Response(200, json=[])

    monkeypatch.setattr(svc.httpx, "get", _fake_get)
    result = svc.run_sanctions_check(db, prof, TENANT_ID)
    assert result == "clear"
    assert prof.sanctions_check_status == "clear"
    assert isinstance(prof.sanctions_checked_at, datetime)
    assert _admin_notifs(db) == []


# ------------------------------------------------------------------ COM chave: hit
def test_key_present_hit_notifies_admins(monkeypatch):
    monkeypatch.setenv("TRANSPARENCIA_API_KEY", "chave-teste")
    db, prof = _build()

    def _fake_get(url, headers=None, params=None, timeout=None):  # noqa: ARG001
        # CEIS devolve 1 sancao; CNEP vazio -> ainda assim e hit.
        if "ceis" in url:
            return httpx.Response(200, json=[{"id": 1, "sancionado": {"cpfFormatado": "***"}}])
        return httpx.Response(200, json=[])

    monkeypatch.setattr(svc.httpx, "get", _fake_get)
    result = svc.run_sanctions_check(db, prof, TENANT_ID)
    assert result == "hit"
    assert prof.sanctions_check_status == "hit"
    assert isinstance(prof.sanctions_checked_at, datetime)
    # Notifica os 2 admins do tenant (admin + super_admin).
    notifs = _admin_notifs(db)
    assert len(notifs) == 2
    # LGPD: nao guarda o dossie — a notificacao nao carrega detalhes da sancao.
    import json as _json
    for n in notifs:
        meta = _json.loads(n.metadata_json or "{}")
        assert "cpf" not in meta and "dossie" not in meta


def test_hit_does_not_change_aggregate_status(monkeypatch):
    """hit NAO vira flagged automatico — decisao humana."""
    monkeypatch.setenv("TRANSPARENCIA_API_KEY", "chave-teste")
    db, prof = _build()
    prof.background_check_status = "verified"

    monkeypatch.setattr(svc.httpx, "get", lambda *a, **k: httpx.Response(200, json=[{"id": 1}]))
    svc.run_sanctions_check(db, prof, TENANT_ID)
    assert prof.sanctions_check_status == "hit"
    assert prof.background_check_status == "verified"  # inalterado


# --------------------------------------------------------- COM chave: FAIL-OPEN
def test_timeout_is_fail_open(monkeypatch):
    monkeypatch.setenv("TRANSPARENCIA_API_KEY", "chave-teste")
    db, prof = _build()

    def _raise(*a, **k):
        raise httpx.TimeoutException("timeout")

    monkeypatch.setattr(svc.httpx, "get", _raise)
    # NUNCA levanta.
    result = svc.run_sanctions_check(db, prof, TENANT_ID)
    assert result == "error"
    assert prof.sanctions_check_status == "error"
    assert _admin_notifs(db) == []


def test_http_error_is_fail_open(monkeypatch):
    monkeypatch.setenv("TRANSPARENCIA_API_KEY", "chave-teste")
    db, prof = _build()
    monkeypatch.setattr(svc.httpx, "get", lambda *a, **k: httpx.Response(500, text="boom"))
    result = svc.run_sanctions_check(db, prof, TENANT_ID)
    assert result == "error"
    assert prof.sanctions_check_status == "error"


def test_no_cpf_is_noop(monkeypatch):
    """Sem CPF nao ha o que consultar — no-op (nao chama a API)."""
    monkeypatch.setenv("TRANSPARENCIA_API_KEY", "chave-teste")
    db, prof = _build()
    prof.cpf = ""

    called = {"n": 0}

    def _get(*a, **k):
        called["n"] += 1
        return httpx.Response(200, json=[])

    monkeypatch.setattr(svc.httpx, "get", _get)
    result = svc.run_sanctions_check(db, prof, TENANT_ID)
    assert result is None
    assert called["n"] == 0
