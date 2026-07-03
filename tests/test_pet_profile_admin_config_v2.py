"""Testes do contrato v2 do endpoint GET/PATCH /api/admin/pet-profile/config.

Cobrem os campos novos:
- platform_enabled  (lê env PET_LIVE_PROFILE_ENABLED)
- tenant_feature_enabled  (lê TenantFeature "pet_live_profile"; ausente = False)
- plan_gates  (bloqueios por plano via FREE_PLAN_BLOCKED_FEATURE_KEYS + trial)
- effective_active  (AND das 3 camadas)
- PATCH com tenant_feature_enabled cria/atualiza TenantFeature
- PATCH sem tenant_feature_enabled não toca TenantFeature
"""
from __future__ import annotations

import app.models  # noqa: F401

import pytest
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.dependencies.auth import get_current_user
from app.models.tenant import Tenant, TenantFeature
from app.models.pet_profile_config import PetProfileConfig
from app.models.user import User
from app.routes import pet_profile as routes


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_db(plan: str = "business", trial_ends_at=None):
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    tenant = Tenant(id="t1", name="T1", slug="t1", status="active", plan=plan)
    if trial_ends_at is not None:
        tenant.trial_ends_at = trial_ends_at
    db.add(tenant)
    db.commit()
    return db


def _client(db):
    admin = User(id="a1", email="a@x.com", password_hash="x", role="super_admin", tenant_id="t1")
    app = FastAPI()
    app.include_router(routes.api_admin_router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: admin
    return TestClient(app)


# ---------------------------------------------------------------------------
# GET — campos read-only presentes
# ---------------------------------------------------------------------------

class TestGetConfigV2:
    def test_get_returns_new_fields_with_defaults(self, monkeypatch):
        """GET deve retornar platform_enabled, tenant_feature_enabled, plan_gates, effective_active."""
        monkeypatch.delenv("PET_LIVE_PROFILE_ENABLED", raising=False)
        db = _make_db()
        c = _client(db)
        r = c.get("/api/admin/pet-profile/config")
        assert r.status_code == 200
        body = r.json()
        assert "platform_enabled" in body
        assert "tenant_feature_enabled" in body
        assert "plan_gates" in body
        assert "effective_active" in body

    def test_platform_enabled_reflects_env_false(self, monkeypatch):
        monkeypatch.delenv("PET_LIVE_PROFILE_ENABLED", raising=False)
        db = _make_db()
        r = _client(db).get("/api/admin/pet-profile/config")
        assert r.json()["platform_enabled"] is False

    def test_platform_enabled_reflects_env_true(self, monkeypatch):
        monkeypatch.setenv("PET_LIVE_PROFILE_ENABLED", "true")
        db = _make_db()
        r = _client(db).get("/api/admin/pet-profile/config")
        assert r.json()["platform_enabled"] is True

    def test_tenant_feature_enabled_absent_is_false(self, monkeypatch):
        """Sem linha em TenantFeature para pet_live_profile → tenant_feature_enabled=False."""
        monkeypatch.delenv("PET_LIVE_PROFILE_ENABLED", raising=False)
        db = _make_db()
        r = _client(db).get("/api/admin/pet-profile/config")
        assert r.json()["tenant_feature_enabled"] is False

    def test_tenant_feature_enabled_present_true(self, monkeypatch):
        monkeypatch.delenv("PET_LIVE_PROFILE_ENABLED", raising=False)
        db = _make_db()
        db.add(TenantFeature(tenant_id="t1", feature_key="pet_live_profile", enabled=True))
        db.commit()
        r = _client(db).get("/api/admin/pet-profile/config")
        assert r.json()["tenant_feature_enabled"] is True

    def test_tenant_feature_enabled_present_false(self, monkeypatch):
        monkeypatch.delenv("PET_LIVE_PROFILE_ENABLED", raising=False)
        db = _make_db()
        db.add(TenantFeature(tenant_id="t1", feature_key="pet_live_profile", enabled=False))
        db.commit()
        r = _client(db).get("/api/admin/pet-profile/config")
        assert r.json()["tenant_feature_enabled"] is False

    def test_effective_active_false_when_all_off(self, monkeypatch):
        monkeypatch.delenv("PET_LIVE_PROFILE_ENABLED", raising=False)
        db = _make_db()
        r = _client(db).get("/api/admin/pet-profile/config")
        assert r.json()["effective_active"] is False

    def test_effective_active_true_requires_all_three_layers(self, monkeypatch):
        monkeypatch.setenv("PET_LIVE_PROFILE_ENABLED", "true")
        db = _make_db()
        # Camada 2: TenantFeature ON
        db.add(TenantFeature(tenant_id="t1", feature_key="pet_live_profile", enabled=True))
        db.commit()
        # Camada 3: profile_enabled ainda False
        r = _client(db).get("/api/admin/pet-profile/config")
        assert r.json()["effective_active"] is False
        # Liga camada 3
        _client(db).patch("/api/admin/pet-profile/config", json={"profile_enabled": True})
        r2 = _client(db).get("/api/admin/pet-profile/config")
        assert r2.json()["effective_active"] is True

    def test_plan_gates_pro_tenant_all_allowed(self, monkeypatch):
        """Plano pro/business: nenhuma feature do Perfil Vivo é bloqueada por plano."""
        monkeypatch.delenv("PET_LIVE_PROFILE_ENABLED", raising=False)
        db = _make_db(plan="pro")
        body = _client(db).get("/api/admin/pet-profile/config").json()
        pg = body["plan_gates"]
        assert pg["alerts_allowed"] is True
        assert pg["share_allowed"] is True
        assert pg["evolution_allowed"] is True

    def test_plan_gates_free_tenant_blocks_pro_features(self, monkeypatch):
        """Plano free fora do trial: pet_alerts e pet_share bloqueados; evolution_allowed=False."""
        monkeypatch.delenv("PET_LIVE_PROFILE_ENABLED", raising=False)
        db = _make_db(plan="free")
        body = _client(db).get("/api/admin/pet-profile/config").json()
        pg = body["plan_gates"]
        assert pg["alerts_allowed"] is False
        assert pg["share_allowed"] is False
        assert pg["evolution_allowed"] is False

    def test_plan_gates_free_tenant_in_trial_all_allowed(self, monkeypatch):
        """Free com trial ativo (plano efetivo = pro): todas as features liberadas."""
        monkeypatch.delenv("PET_LIVE_PROFILE_ENABLED", raising=False)
        future = datetime.utcnow() + timedelta(days=7)
        db = _make_db(plan="free", trial_ends_at=future)
        body = _client(db).get("/api/admin/pet-profile/config").json()
        pg = body["plan_gates"]
        assert pg["alerts_allowed"] is True
        assert pg["share_allowed"] is True
        assert pg["evolution_allowed"] is True

    def test_plan_gates_shape(self, monkeypatch):
        """plan_gates sempre tem as chaves esperadas."""
        monkeypatch.delenv("PET_LIVE_PROFILE_ENABLED", raising=False)
        db = _make_db(plan="business")
        body = _client(db).get("/api/admin/pet-profile/config").json()
        pg = body["plan_gates"]
        assert set(pg.keys()) == {"plan", "alerts_allowed", "share_allowed", "evolution_allowed"}

    # --- Regressão: campos antigos ainda presentes ---
    def test_old_fields_still_present(self, monkeypatch):
        monkeypatch.delenv("PET_LIVE_PROFILE_ENABLED", raising=False)
        db = _make_db()
        body = _client(db).get("/api/admin/pet-profile/config").json()
        for key in ("tenant_id", "profile_enabled", "observations_enabled",
                    "reminders_enabled", "vaccine_lead_days", "inactivity_days", "share_enabled"):
            assert key in body, f"campo {key!r} ausente na resposta"


# ---------------------------------------------------------------------------
# PATCH — tenant_feature_enabled
# ---------------------------------------------------------------------------

class TestPatchConfigV2:
    def test_patch_with_tenant_feature_enabled_true_creates_row(self, monkeypatch):
        """PATCH com tenant_feature_enabled=True deve criar TenantFeature e refletir na resposta."""
        monkeypatch.delenv("PET_LIVE_PROFILE_ENABLED", raising=False)
        db = _make_db()
        c = _client(db)
        r = c.patch("/api/admin/pet-profile/config", json={"tenant_feature_enabled": True})
        assert r.status_code == 200
        body = r.json()
        assert body["tenant_feature_enabled"] is True
        # Confirma que a linha foi gravada no banco
        row = db.query(TenantFeature).filter_by(tenant_id="t1", feature_key="pet_live_profile").first()
        assert row is not None
        assert row.enabled is True

    def test_patch_with_tenant_feature_enabled_false_creates_row_disabled(self, monkeypatch):
        monkeypatch.delenv("PET_LIVE_PROFILE_ENABLED", raising=False)
        db = _make_db()
        c = _client(db)
        r = c.patch("/api/admin/pet-profile/config", json={"tenant_feature_enabled": False})
        assert r.status_code == 200
        assert r.json()["tenant_feature_enabled"] is False
        row = db.query(TenantFeature).filter_by(tenant_id="t1", feature_key="pet_live_profile").first()
        assert row is not None
        assert row.enabled is False

    def test_patch_tenant_feature_enabled_updates_existing_row(self, monkeypatch):
        """Se já existe TenantFeature, o PATCH faz UPDATE (sem violar unique constraint)."""
        monkeypatch.delenv("PET_LIVE_PROFILE_ENABLED", raising=False)
        db = _make_db()
        db.add(TenantFeature(tenant_id="t1", feature_key="pet_live_profile", enabled=False))
        db.commit()
        c = _client(db)
        r = c.patch("/api/admin/pet-profile/config", json={"tenant_feature_enabled": True})
        assert r.status_code == 200
        assert r.json()["tenant_feature_enabled"] is True
        rows = db.query(TenantFeature).filter_by(tenant_id="t1", feature_key="pet_live_profile").all()
        assert len(rows) == 1  # sem duplicata
        assert rows[0].enabled is True

    def test_patch_without_tenant_feature_enabled_does_not_touch_feature_table(self, monkeypatch):
        """PATCH sem tenant_feature_enabled não cria/modifica TenantFeature."""
        monkeypatch.delenv("PET_LIVE_PROFILE_ENABLED", raising=False)
        db = _make_db()
        c = _client(db)
        r = c.patch("/api/admin/pet-profile/config", json={"profile_enabled": True})
        assert r.status_code == 200
        rows = db.query(TenantFeature).filter_by(tenant_id="t1", feature_key="pet_live_profile").all()
        assert len(rows) == 0  # nenhuma linha criada

    def test_patch_tenant_feature_enabled_and_profile_enabled_together(self, monkeypatch):
        """PATCH pode atualizar profile_enabled e tenant_feature_enabled na mesma chamada."""
        monkeypatch.setenv("PET_LIVE_PROFILE_ENABLED", "true")
        db = _make_db()
        c = _client(db)
        r = c.patch("/api/admin/pet-profile/config", json={
            "profile_enabled": True,
            "tenant_feature_enabled": True,
        })
        assert r.status_code == 200
        body = r.json()
        assert body["profile_enabled"] is True
        assert body["tenant_feature_enabled"] is True
        assert body["effective_active"] is True

    def test_patch_response_contains_all_new_fields(self, monkeypatch):
        """Resposta do PATCH tem o mesmo shape do GET (com campos novos)."""
        monkeypatch.delenv("PET_LIVE_PROFILE_ENABLED", raising=False)
        db = _make_db()
        r = _client(db).patch("/api/admin/pet-profile/config", json={"profile_enabled": False})
        body = r.json()
        for key in ("platform_enabled", "tenant_feature_enabled", "plan_gates", "effective_active"):
            assert key in body, f"campo {key!r} ausente na resposta do PATCH"

    def test_patch_empty_body_is_backward_compatible(self, monkeypatch):
        """PATCH com body vazio (ou só campos antigos) continua retornando 200 — retrocompatível."""
        monkeypatch.delenv("PET_LIVE_PROFILE_ENABLED", raising=False)
        db = _make_db()
        r = _client(db).patch("/api/admin/pet-profile/config", json={})
        assert r.status_code == 200

    def test_effective_active_becomes_true_after_full_enable(self, monkeypatch):
        """Liga as 3 camadas via PATCH e confirma effective_active=True."""
        monkeypatch.setenv("PET_LIVE_PROFILE_ENABLED", "true")
        db = _make_db()
        c = _client(db)
        r = c.patch("/api/admin/pet-profile/config", json={
            "profile_enabled": True,
            "tenant_feature_enabled": True,
        })
        assert r.json()["effective_active"] is True
