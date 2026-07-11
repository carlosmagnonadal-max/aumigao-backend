from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.exc import IntegrityError
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.models  # noqa: F401 — registra todas as tabelas no Base.metadata
from app.core.database import Base, get_db, get_tutor_self_db
from app.dependencies.auth import get_current_user
from app.models.tenant import Tenant
from app.models.user import User
from app.models.tenant_tutor_access import TenantTutorAccess

TENANT_A, TENANT_B, TUTOR = "tt-a", "tt-b", "tt-tutor"


def test_multi_tenant_tutor_flag(monkeypatch):
    from app.core import feature_flags
    monkeypatch.delenv("MULTI_TENANT_TUTOR", raising=False)
    assert feature_flags.multi_tenant_tutor_enabled() is False
    monkeypatch.setenv("MULTI_TENANT_TUTOR", "true")
    assert feature_flags.multi_tenant_tutor_enabled() is True


def _db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_is_tutor_eligible_for_tenant():
    from app.services.tutor_network_service import is_tutor_eligible_for_tenant
    db = _db()
    db.add(TenantTutorAccess(tenant_id="t1", tutor_user_id="u1", status="active"))
    db.add(TenantTutorAccess(tenant_id="t2", tutor_user_id="u1", status="revoked"))
    db.commit()
    assert is_tutor_eligible_for_tenant(db, "t1", "u1") is True
    assert is_tutor_eligible_for_tenant(db, "t2", "u1") is False
    assert is_tutor_eligible_for_tenant(db, "t9", "u1") is False


def test_tenant_tutor_access_defaults():
    db = _db()
    row = TenantTutorAccess(tenant_id="t1", tutor_user_id="u1")
    db.add(row); db.commit(); db.refresh(row)
    assert row.id
    assert row.status == "active"
    assert row.initiated_by == "tutor"
    assert row.created_at is not None


def test_tenant_tutor_access_unique_constraint():
    db = _db()
    db.add(TenantTutorAccess(tenant_id="t1", tutor_user_id="u1")); db.commit()
    db.add(TenantTutorAccess(tenant_id="t1", tutor_user_id="u1"))
    with pytest.raises(IntegrityError):
        db.commit()


# ─── Router tests: GET /tutor/tenants + POST /tutor/tenants/join ──────────────


def _seed(db):
    db.add(Tenant(id=TENANT_A, name="Rede A", slug="rede-a", status="active", plan="starter"))
    db.add(Tenant(id=TENANT_B, name="Rede B", slug="rede-b", status="active", plan="starter"))
    db.add(User(id=TUTOR, email="t@x.com", password_hash="x", role="tutor", tenant_id=TENANT_A))
    db.add(TenantTutorAccess(tenant_id=TENANT_A, tutor_user_id=TUTOR, status="active"))
    db.commit()


def _client(db, flag_on, monkeypatch):
    monkeypatch.setenv("MULTI_TENANT_TUTOR", "true" if flag_on else "false")
    import app.routes.tutor as tutor_module
    fastapi_app = FastAPI()
    fastapi_app.include_router(tutor_module.router)
    tutor = db.get(User, TUTOR)
    fastapi_app.dependency_overrides[get_db] = lambda: db
    fastapi_app.dependency_overrides[get_tutor_self_db] = lambda: db
    fastapi_app.dependency_overrides[get_current_user] = lambda: tutor
    return TestClient(fastapi_app, raise_server_exceptions=True)


def test_tutor_tenants_lista_so_active(monkeypatch):
    db = _db(); _seed(db)
    r = _client(db, True, monkeypatch).get("/tutor/tenants")
    assert r.status_code == 200
    assert [t["tenant_id"] for t in r.json()] == [TENANT_A]


def test_tutor_tenants_inclui_tenant_nativo_sem_vinculo(monkeypatch):
    """Incidente 11/07: tutor do próprio tenant não via a marca dele no app —
    o tenant nativo (user.tenant_id) não entrava na lista, então o app não
    tinha o que auto-ativar. O nativo entra como vínculo implícito ativo."""
    db = _db()
    db.add(Tenant(id=TENANT_A, name="Rede A", slug="rede-a", status="active", plan="starter"))
    db.add(User(id=TUTOR, email="t@x.com", password_hash="x", role="tutor", tenant_id=TENANT_A))
    db.commit()
    r = _client(db, True, monkeypatch).get("/tutor/tenants")
    assert r.status_code == 200
    body = r.json()
    assert [t["tenant_id"] for t in body] == [TENANT_A]
    assert body[0]["access_status"] == "active"


def test_tutor_tenants_nativo_primeiro_e_sem_duplicar(monkeypatch):
    """Nativo vem primeiro; vínculo explícito com o próprio nativo não duplica."""
    db = _db()
    db.add(Tenant(id=TENANT_A, name="Rede A", slug="rede-a", status="active", plan="starter"))
    db.add(Tenant(id=TENANT_B, name="Rede B", slug="rede-b", status="active", plan="starter"))
    db.add(User(id=TUTOR, email="t@x.com", password_hash="x", role="tutor", tenant_id=TENANT_A))
    db.add(TenantTutorAccess(tenant_id=TENANT_B, tutor_user_id=TUTOR, status="active"))
    db.commit()
    r = _client(db, True, monkeypatch).get("/tutor/tenants")
    assert [t["tenant_id"] for t in r.json()] == [TENANT_A, TENANT_B]


def test_tutor_tenants_flag_off_vazio(monkeypatch):
    db = _db(); _seed(db)
    r = _client(db, False, monkeypatch).get("/tutor/tenants")
    assert r.status_code == 200 and r.json() == []


def test_tutor_join_cria_e_idempotente(monkeypatch):
    db = _db(); _seed(db)
    c = _client(db, True, monkeypatch)
    r1 = c.post("/tutor/tenants/join", json={"tenant_slug": "rede-b"})
    assert r1.status_code == 200 and r1.json()["slug"] == "rede-b"
    r2 = c.post("/tutor/tenants/join", json={"tenant_slug": "rede-b"})
    assert r2.status_code == 200
    assert db.query(TenantTutorAccess).filter_by(tenant_id=TENANT_B, tutor_user_id=TUTOR).count() == 1


def test_tutor_join_tenant_inexistente(monkeypatch):
    db = _db(); _seed(db)
    r = _client(db, True, monkeypatch).post("/tutor/tenants/join", json={"tenant_slug": "nao-existe"})
    assert r.status_code == 404


def test_tutor_join_flag_off_404(monkeypatch):
    db = _db(); _seed(db)
    r = _client(db, False, monkeypatch).post("/tutor/tenants/join", json={"tenant_slug": "rede-b"})
    assert r.status_code == 404


# ─── Gate de vínculo (unidade do serviço) ─────────────────────────────────────


def test_gate_reserva_tutor():
    from app.services.tutor_network_service import is_tutor_eligible_for_tenant
    db = _db()
    db.add(TenantTutorAccess(tenant_id="t-vinculado", tutor_user_id="u1", status="active"))
    db.commit()
    assert is_tutor_eligible_for_tenant(db, "t-vinculado", "u1") is True
    assert is_tutor_eligible_for_tenant(db, "t-outro", "u1") is False
