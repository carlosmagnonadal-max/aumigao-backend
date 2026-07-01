"""T16 — Testes das rotas de compartilhamento do perfil do pet (Fase 4 LGPD)."""
from __future__ import annotations

import app.models  # noqa: F401

from datetime import datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.dependencies.auth import get_current_user
from app.models.pet import Pet
from app.models.pet_profile_config import PetProfileConfig
from app.models.pet_share_link import PetShareLink
from app.models.pet_timeline_event import PetTimelineEvent
from app.models.tenant import Tenant, TenantFeature
from app.models.user import User
from app.routes import pet_share as routes


def _ctx(share_active=True, profile_active=True):
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()

    db.add(Tenant(id="t1", name="T1", slug="t1", status="active", plan="business"))
    db.add(User(id="u1", email="u1@x.com", password_hash="x", role="tutor", tenant_id="t1"))
    db.add(User(id="u2", email="u2@x.com", password_hash="x", role="tutor", tenant_id="t1"))
    db.add(Pet(
        id="p1", tutor_id="u1", tenant_id="t1", name="Rex Silva",
        birth_date=datetime(2022, 1, 1).date(),
        allergies="frango", medications="remédio X",
        health_notes="saudável", vet_name="Dr. Vet", vet_phone="71999",
        emergency_contact="João 71888", chip_number="CHIP123",
        species="Cachorro", breed="Labrador", size="grande",
    ))

    if profile_active:
        db.add(TenantFeature(tenant_id="t1", feature_key="pet_live_profile", enabled=True))
    if share_active:
        db.add(TenantFeature(tenant_id="t1", feature_key="pet_share", enabled=True))
    if profile_active or share_active:
        db.add(PetProfileConfig(
            tenant_id="t1",
            profile_enabled=bool(profile_active),
            share_enabled=bool(share_active),
        ))

    db.commit()
    return db


def _client(db, user, env_profile=True, env_share=True, monkeypatch=None):
    if monkeypatch:
        if env_profile:
            monkeypatch.setenv("PET_LIVE_PROFILE_ENABLED", "true")
        else:
            monkeypatch.delenv("PET_LIVE_PROFILE_ENABLED", raising=False)
        if env_share:
            monkeypatch.setenv("PET_SHARE_ENABLED", "true")
        else:
            monkeypatch.delenv("PET_SHARE_ENABLED", raising=False)

    app = FastAPI()
    app.include_router(routes.api_router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app)


def _public_client(db, env_share=True, monkeypatch=None):
    """Cliente para rota pública (sem override de auth)."""
    if monkeypatch:
        if env_share:
            monkeypatch.setenv("PET_SHARE_ENABLED", "true")
        else:
            monkeypatch.delenv("PET_SHARE_ENABLED", raising=False)

    from unittest.mock import patch, MagicMock
    from contextlib import contextmanager
    from sqlalchemy.orm import Session

    # Mock global_scope_session para usar o DB de teste (evita conexão real)
    @contextmanager
    def _mock_global_scope():
        db.info["rls_tenant"] = "*"
        yield db

    app = FastAPI()
    app.include_router(routes.public_router)

    with patch("app.routes.pet_share.global_scope_session", _mock_global_scope):
        client = TestClient(app, raise_server_exceptions=True)
        return client, _mock_global_scope


# ---------------------------------------------------------------------------
# POST /api/pets/{pet_id}/share-link
# ---------------------------------------------------------------------------

def test_create_share_link_returns_token(monkeypatch):
    """Tutor dono + gates ON + consent=true → 200, token e URL."""
    db = _ctx()
    c = _client(db, db.get(User, "u1"), monkeypatch=monkeypatch)

    r = c.post("/api/pets/p1/share-link", json={"consent": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "token" in body
    assert body["url"].startswith("https://app.aumigaowalk.com.br/pet/")
    assert "expires_at" in body


def test_consent_false_returns_422(monkeypatch):
    """consent=false → 422 com mensagem clara."""
    db = _ctx()
    c = _client(db, db.get(User, "u1"), monkeypatch=monkeypatch)

    r = c.post("/api/pets/p1/share-link", json={"consent": False})
    assert r.status_code == 422
    body = r.json()
    assert "LGPD" in str(body) or "consentimento" in str(body).lower()


def test_consent_missing_returns_422(monkeypatch):
    """Body sem consent → 422."""
    db = _ctx()
    c = _client(db, db.get(User, "u1"), monkeypatch=monkeypatch)

    r = c.post("/api/pets/p1/share-link", json={})
    assert r.status_code == 422


def test_feature_off_returns_404(monkeypatch):
    """share feature OFF → 404 no POST."""
    db = _ctx(share_active=False)
    c = _client(db, db.get(User, "u1"), env_share=False, monkeypatch=monkeypatch)

    r = c.post("/api/pets/p1/share-link", json={"consent": True})
    assert r.status_code == 404


def test_profile_feature_off_returns_404(monkeypatch):
    """profile feature OFF → 404 mesmo com share feature ON."""
    db = _ctx(profile_active=False, share_active=True)
    c = _client(db, db.get(User, "u1"), env_profile=False, env_share=True, monkeypatch=monkeypatch)

    r = c.post("/api/pets/p1/share-link", json={"consent": True})
    assert r.status_code == 404


def test_non_owner_returns_404(monkeypatch):
    """Usuário não-dono do pet → 404 (mesmo padrão de _get_owned_pet)."""
    db = _ctx()
    c = _client(db, db.get(User, "u2"), monkeypatch=monkeypatch)

    r = c.post("/api/pets/p1/share-link", json={"consent": True})
    assert r.status_code == 404


def test_second_post_reuses_link(monkeypatch):
    """POST 2x → reaproveita link ativo (mesmo token)."""
    db = _ctx()
    c = _client(db, db.get(User, "u1"), monkeypatch=monkeypatch)

    r1 = c.post("/api/pets/p1/share-link", json={"consent": True})
    assert r1.status_code == 200

    r2 = c.post("/api/pets/p1/share-link", json={"consent": True})
    assert r2.status_code == 200

    assert r1.json()["token"] == r2.json()["token"]
    assert db.query(PetShareLink).filter(PetShareLink.pet_id == "p1").count() == 1


# ---------------------------------------------------------------------------
# DELETE /api/pets/{pet_id}/share-link
# ---------------------------------------------------------------------------

def test_delete_revokes_active_links(monkeypatch):
    """DELETE revoga link ativo → revoked_at preenchido."""
    db = _ctx()
    c = _client(db, db.get(User, "u1"), monkeypatch=monkeypatch)

    c.post("/api/pets/p1/share-link", json={"consent": True})
    assert db.query(PetShareLink).filter(PetShareLink.pet_id == "p1",
                                         PetShareLink.revoked_at.is_(None)).count() == 1

    r = c.delete("/api/pets/p1/share-link")
    assert r.status_code == 200
    assert r.json() == {"ok": True}

    db.expire_all()
    assert db.query(PetShareLink).filter(PetShareLink.pet_id == "p1",
                                         PetShareLink.revoked_at.is_(None)).count() == 0


# ---------------------------------------------------------------------------
# GET /public/pet/{token} — rota pública
# ---------------------------------------------------------------------------

def _make_link(db, pet_id="p1", revoked=False, expired=False):
    now = datetime.utcnow()
    if expired:
        expires_at = now - timedelta(hours=1)
    else:
        expires_at = now + timedelta(days=30)
    link = PetShareLink(
        id=str(__import__("uuid").uuid4()),
        token="tok_test_pub",
        pet_id=pet_id,
        tenant_id="t1",
        created_by="u1",
        consent_at=now,
        expires_at=expires_at,
        revoked_at=now if revoked else None,
        created_at=now,
    )
    db.add(link)
    db.commit()
    return link


def test_public_route_returns_sanitized_payload(monkeypatch):
    """Token válido → 200 com payload sanitizado."""
    db = _ctx()
    monkeypatch.setenv("PET_SHARE_ENABLED", "true")
    link = _make_link(db)

    from unittest.mock import patch
    from contextlib import contextmanager

    @contextmanager
    def _mock_global_scope():
        db.info["rls_tenant"] = "*"
        yield db

    app = FastAPI()
    app.include_router(routes.public_router)
    with patch("app.routes.pet_share.global_scope_session", _mock_global_scope):
        c = TestClient(app)
        r = c.get(f"/public/pet/{link.token}")

    assert r.status_code == 200, r.text
    body = r.json()
    # Inclui — chaves alinhadas ao contrato do site (review P1)
    assert body["pet_first_name"] == "Rex"
    assert "pet_photo_url" in body
    assert "latest_weight_kg" in body
    assert "allergies" in body
    assert "medications" in body
    assert "health_notes" in body
    assert "vet_name" in body
    assert "timeline" in body
    assert "tenant" in body
    # Chaves antigas NÃO existem mais (review P1)
    assert "photo_url" not in body
    assert "weight_kg" not in body
    # Idade calculada como OBJETO {years, months}, NUNCA birth_date cru
    assert "age" in body
    age = body["age"]
    assert isinstance(age, dict)  # birth_date 2022-01-01 → objeto
    assert isinstance(age["years"], int)
    assert isinstance(age["months"], int)
    assert age["years"] >= 4  # nascido 2022-01-01, hoje >= 2026
    # Exclui dados sensíveis
    assert "emergency_contact" not in body
    assert "chip_number" not in body
    assert "tutor_id" not in body
    assert "birth_date" not in body
    # Não deve conter valores sensíveis no corpo inteiro
    body_str = str(body)
    assert "CHIP123" not in body_str
    assert "João 71888" not in body_str


def test_public_route_excludes_walk_observation_events(monkeypatch):
    """Eventos walk_observation NÃO aparecem na timeline pública."""
    db = _ctx()
    monkeypatch.setenv("PET_SHARE_ENABLED", "true")
    link = _make_link(db)

    # Insere eventos: walk_observation (deve ser excluído) e vaccine (deve aparecer)
    db.add(PetTimelineEvent(
        pet_id="p1", tenant_id="t1", event_type="walk_observation",
        title="Obs passeio", occurred_at=datetime(2026, 6, 1),
        source="walker",
    ))
    db.add(PetTimelineEvent(
        pet_id="p1", tenant_id="t1", event_type="vaccine",
        title="Vacina V8", occurred_at=datetime(2026, 5, 1),
        source="tutor",
    ))
    db.commit()

    from unittest.mock import patch
    from contextlib import contextmanager

    @contextmanager
    def _mock_global_scope():
        db.info["rls_tenant"] = "*"
        yield db

    app = FastAPI()
    app.include_router(routes.public_router)
    with patch("app.routes.pet_share.global_scope_session", _mock_global_scope):
        c = TestClient(app)
        r = c.get(f"/public/pet/{link.token}")

    assert r.status_code == 200
    timeline = r.json()["timeline"]
    event_types = [e["event_type"] for e in timeline]
    assert "walk_observation" not in event_types
    assert "vaccine" in event_types


def test_public_no_internal_ids_in_timeline(monkeypatch):
    """Timeline pública NÃO expõe id do evento, walk_id, user_id, tenant_id."""
    db = _ctx()
    monkeypatch.setenv("PET_SHARE_ENABLED", "true")
    link = _make_link(db)

    db.add(PetTimelineEvent(
        pet_id="p1", tenant_id="t1", event_type="weight",
        title="Peso 10kg", occurred_at=datetime(2026, 6, 1),
        source="tutor", payload_json='{"kg": 10.0}',
    ))
    db.commit()

    from unittest.mock import patch
    from contextlib import contextmanager

    @contextmanager
    def _mock_global_scope():
        db.info["rls_tenant"] = "*"
        yield db

    app = FastAPI()
    app.include_router(routes.public_router)
    with patch("app.routes.pet_share.global_scope_session", _mock_global_scope):
        c = TestClient(app)
        r = c.get(f"/public/pet/{link.token}")

    assert r.status_code == 200
    for ev in r.json()["timeline"]:
        assert "id" not in ev
        assert "walk_id" not in ev
        assert "user_id" not in ev
        assert "created_by_user_id" not in ev
        assert "tenant_id" not in ev


def test_public_revoked_returns_410(monkeypatch):
    """Token revogado → 410."""
    db = _ctx()
    monkeypatch.setenv("PET_SHARE_ENABLED", "true")
    link = _make_link(db, revoked=True)

    from unittest.mock import patch
    from contextlib import contextmanager

    @contextmanager
    def _mock_global_scope():
        db.info["rls_tenant"] = "*"
        yield db

    app = FastAPI()
    app.include_router(routes.public_router)
    with patch("app.routes.pet_share.global_scope_session", _mock_global_scope):
        c = TestClient(app)
        r = c.get(f"/public/pet/{link.token}")

    assert r.status_code == 410


def test_public_expired_returns_410(monkeypatch):
    """Token expirado → 410."""
    db = _ctx()
    monkeypatch.setenv("PET_SHARE_ENABLED", "true")
    link = _make_link(db, expired=True)

    from unittest.mock import patch
    from contextlib import contextmanager

    @contextmanager
    def _mock_global_scope():
        db.info["rls_tenant"] = "*"
        yield db

    app = FastAPI()
    app.include_router(routes.public_router)
    with patch("app.routes.pet_share.global_scope_session", _mock_global_scope):
        c = TestClient(app)
        r = c.get(f"/public/pet/{link.token}")

    assert r.status_code == 410


def test_public_token_not_found_returns_404(monkeypatch):
    """Token inexistente → 404."""
    db = _ctx()
    monkeypatch.setenv("PET_SHARE_ENABLED", "true")

    from unittest.mock import patch
    from contextlib import contextmanager

    @contextmanager
    def _mock_global_scope():
        db.info["rls_tenant"] = "*"
        yield db

    app = FastAPI()
    app.include_router(routes.public_router)
    with patch("app.routes.pet_share.global_scope_session", _mock_global_scope):
        c = TestClient(app)
        r = c.get("/public/pet/token_nao_existe")

    assert r.status_code == 404


def test_public_env_off_returns_404(monkeypatch):
    """Env PET_SHARE_ENABLED off → 404 mesmo com token válido."""
    db = _ctx()
    monkeypatch.delenv("PET_SHARE_ENABLED", raising=False)
    link = _make_link(db)

    from unittest.mock import patch
    from contextlib import contextmanager

    @contextmanager
    def _mock_global_scope():
        db.info["rls_tenant"] = "*"
        yield db

    app = FastAPI()
    app.include_router(routes.public_router)
    with patch("app.routes.pet_share.global_scope_session", _mock_global_scope):
        c = TestClient(app)
        r = c.get(f"/public/pet/{link.token}")

    assert r.status_code == 404


def test_delete_then_public_returns_410(monkeypatch):
    """DELETE → público retorna 410."""
    db = _ctx()
    c = _client(db, db.get(User, "u1"), monkeypatch=monkeypatch)

    r1 = c.post("/api/pets/p1/share-link", json={"consent": True})
    assert r1.status_code == 200
    token = r1.json()["token"]

    r_del = c.delete("/api/pets/p1/share-link")
    assert r_del.status_code == 200

    from unittest.mock import patch
    from contextlib import contextmanager

    @contextmanager
    def _mock_global_scope():
        db.info["rls_tenant"] = "*"
        db.expire_all()
        yield db

    app2 = FastAPI()
    app2.include_router(routes.public_router)
    with patch("app.routes.pet_share.global_scope_session", _mock_global_scope):
        c2 = TestClient(app2)
        r_pub = c2.get(f"/public/pet/{token}")

    assert r_pub.status_code == 410


# ---------------------------------------------------------------------------
# Review P0 — rota /api/public/pet/{token} (o site consome esse caminho)
# ---------------------------------------------------------------------------

def test_api_public_route_exact_path(monkeypatch):
    """GET exatamente em /api/public/pet/{token} → 200 (contrato do site)."""
    db = _ctx()
    monkeypatch.setenv("PET_SHARE_ENABLED", "true")
    link = _make_link(db)

    from unittest.mock import patch
    from contextlib import contextmanager

    @contextmanager
    def _mock_global_scope():
        db.info["rls_tenant"] = "*"
        yield db

    app = FastAPI()
    app.include_router(routes.api_router)
    with patch("app.routes.pet_share.global_scope_session", _mock_global_scope):
        c = TestClient(app)
        r = c.get(f"/api/public/pet/{link.token}")

    assert r.status_code == 200, r.text
    assert r.json()["pet_first_name"] == "Rex"


# ---------------------------------------------------------------------------
# Review P1 — age null sem birth_date
# ---------------------------------------------------------------------------

def test_public_age_null_without_birth_date(monkeypatch):
    """Pet sem birth_date → age null no payload público."""
    db = _ctx()
    monkeypatch.setenv("PET_SHARE_ENABLED", "true")
    pet = db.get(Pet, "p1")
    pet.birth_date = None
    db.commit()
    link = _make_link(db)

    from unittest.mock import patch
    from contextlib import contextmanager

    @contextmanager
    def _mock_global_scope():
        db.info["rls_tenant"] = "*"
        yield db

    app = FastAPI()
    app.include_router(routes.public_router)
    with patch("app.routes.pet_share.global_scope_session", _mock_global_scope):
        c = TestClient(app)
        r = c.get(f"/public/pet/{link.token}")

    assert r.status_code == 200
    assert r.json()["age"] is None


# ---------------------------------------------------------------------------
# Review P2 — allow-list de chaves do payload_json na timeline pública
# ---------------------------------------------------------------------------

def test_public_timeline_payload_allowlist_filters_extra_keys(monkeypatch):
    """Evento weight com chaves extras no payload → público só contém 'kg'."""
    import json as _json

    db = _ctx()
    monkeypatch.setenv("PET_SHARE_ENABLED", "true")
    link = _make_link(db)

    db.add(PetTimelineEvent(
        pet_id="p1", tenant_id="t1", event_type="weight",
        title="Peso", occurred_at=datetime(2026, 6, 1), source="tutor",
        payload_json=_json.dumps({"kg": 12, "qualquer_outra": "x"}),
    ))
    db.commit()

    from unittest.mock import patch
    from contextlib import contextmanager

    @contextmanager
    def _mock_global_scope():
        db.info["rls_tenant"] = "*"
        yield db

    app = FastAPI()
    app.include_router(routes.public_router)
    with patch("app.routes.pet_share.global_scope_session", _mock_global_scope):
        c = TestClient(app)
        r = c.get(f"/public/pet/{link.token}")

    assert r.status_code == 200
    timeline = r.json()["timeline"]
    weight_events = [e for e in timeline if e["event_type"] == "weight"]
    assert len(weight_events) == 1
    payload = _json.loads(weight_events[0]["payload_json"])
    assert payload == {"kg": 12}
    assert "qualquer_outra" not in weight_events[0]["payload_json"]


def test_public_timeline_payload_malformed_becomes_null(monkeypatch):
    """Payload malformado → payload_json null no público (não vaza cru)."""
    db = _ctx()
    monkeypatch.setenv("PET_SHARE_ENABLED", "true")
    link = _make_link(db)

    db.add(PetTimelineEvent(
        pet_id="p1", tenant_id="t1", event_type="weight",
        title="Peso quebrado", occurred_at=datetime(2026, 6, 1), source="tutor",
        payload_json="not-json{{{",
    ))
    db.commit()

    from unittest.mock import patch
    from contextlib import contextmanager

    @contextmanager
    def _mock_global_scope():
        db.info["rls_tenant"] = "*"
        yield db

    app = FastAPI()
    app.include_router(routes.public_router)
    with patch("app.routes.pet_share.global_scope_session", _mock_global_scope):
        c = TestClient(app)
        r = c.get(f"/public/pet/{link.token}")

    assert r.status_code == 200
    timeline = r.json()["timeline"]
    weight_events = [e for e in timeline if e["event_type"] == "weight"]
    assert len(weight_events) == 1
    assert weight_events[0]["payload_json"] is None
    assert "not-json" not in str(r.json())


def test_public_timeline_health_note_payload_always_null(monkeypatch):
    """health_note: allow-list vazia → payload_json sempre null no público."""
    import json as _json

    db = _ctx()
    monkeypatch.setenv("PET_SHARE_ENABLED", "true")
    link = _make_link(db)

    db.add(PetTimelineEvent(
        pet_id="p1", tenant_id="t1", event_type="health_note",
        title="Nota", occurred_at=datetime(2026, 6, 1), source="tutor",
        payload_json=_json.dumps({"detalhe_sensivel": "abc"}),
    ))
    db.commit()

    from unittest.mock import patch
    from contextlib import contextmanager

    @contextmanager
    def _mock_global_scope():
        db.info["rls_tenant"] = "*"
        yield db

    app = FastAPI()
    app.include_router(routes.public_router)
    with patch("app.routes.pet_share.global_scope_session", _mock_global_scope):
        c = TestClient(app)
        r = c.get(f"/public/pet/{link.token}")

    assert r.status_code == 200
    timeline = r.json()["timeline"]
    hn = [e for e in timeline if e["event_type"] == "health_note"]
    assert len(hn) == 1
    assert hn[0]["payload_json"] is None
    assert "detalhe_sensivel" not in str(r.json())
