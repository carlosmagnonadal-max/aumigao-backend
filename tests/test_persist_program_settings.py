"""Testes de round-trip para a persistencia de configuracoes de programa no banco.

Verifica que:
1. get_setting() retorna o default quando nao ha nada salvo (base vazia).
2. save_setting() + get_setting() fazem round-trip correto.
3. _merge_dict aninhado e preservado (semantica existente).
4. append_walker_program_action() persiste e recent_walker_program_actions() le.
5. Os endpoints HTTP de settings fazem GET/PUT com persistencia real.

Padrao do projeto: SQLite em memoria, FastAPI minimo, override de get_db.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 — registra todas as tabelas em Base.metadata
from app.core.database import Base, get_db
from app.dependencies.auth import get_current_user
from app.models.user import User
from app.routes import admin
from app.services.app_settings_service import (
    append_walker_program_action,
    get_setting,
    recent_walker_program_actions,
    save_setting,
)

SUPER_ID = "super-persist-1"


def _make_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine


def _make_session(engine):
    return sessionmaker(bind=engine)()


def _make_client(db):
    """FastAPI minimo com admin router e overrides de DB e auth."""
    app_ = FastAPI()
    app_.include_router(admin.router)
    app_.dependency_overrides[get_db] = lambda: db
    app_.dependency_overrides[get_current_user] = lambda: db.get(User, SUPER_ID)
    return TestClient(app_)


def _seed_super(db):
    db.add(User(id=SUPER_ID, email="super@aumigao.app", password_hash="x", role="super_admin"))
    db.commit()


# -----------------------------------------------------------------------
# Testes de servico (camada de service, sem HTTP)
# -----------------------------------------------------------------------

class TestGetSettingDefault:
    """Numa base vazia, get_setting retorna o default intacto."""

    def test_returns_default_when_no_row(self):
        db = _make_session(_make_engine())
        default = {"foo": "bar", "nested": {"a": 1}}
        result = get_setting(db, "any_key", default)
        assert result == default

    def test_does_not_mutate_default(self):
        db = _make_session(_make_engine())
        default = {"x": 10}
        result = get_setting(db, "key", default)
        result["x"] = 99
        assert default["x"] == 10  # default original intacto


class TestSaveAndGetSetting:
    """save_setting persiste e get_setting le de volta com merge correto."""

    def test_roundtrip_flat(self):
        db = _make_session(_make_engine())
        default = {"enabled": False, "amount": 10}
        save_setting(db, "prog", {"enabled": True, "amount": 20}, updated_by="test")
        result = get_setting(db, "prog", default)
        assert result["enabled"] is True
        assert result["amount"] == 20

    def test_partial_update_merges_with_default(self):
        """Atualizar so um campo deve manter os demais do default."""
        db = _make_session(_make_engine())
        default = {"enabled": False, "amount": 50, "name": "default"}
        save_setting(db, "prog", {"enabled": True}, updated_by="test")
        result = get_setting(db, "prog", default)
        assert result["enabled"] is True
        assert result["amount"] == 50  # veio do default
        assert result["name"] == "default"  # veio do default

    def test_nested_merge_deep(self):
        """Merge profundo: subdict salvo sobrescreve apenas as chaves alteradas."""
        db = _make_session(_make_engine())
        default = {
            "client_rules": {
                "discount": 20,
                "limit": 10,
            },
            "enabled": False,
        }
        saved = {"client_rules": {"discount": 30}}
        save_setting(db, "prog", saved, updated_by="test")
        result = get_setting(db, "prog", default)
        assert result["client_rules"]["discount"] == 30
        assert result["client_rules"]["limit"] == 10  # default do subdict preservado
        assert result["enabled"] is False

    def test_upsert_overwrites_previous(self):
        db = _make_session(_make_engine())
        default = {"val": 0}
        save_setting(db, "prog", {"val": 1}, updated_by="a")
        save_setting(db, "prog", {"val": 2}, updated_by="b")
        result = get_setting(db, "prog", default)
        assert result["val"] == 2

    def test_updated_by_stored(self):
        db = _make_session(_make_engine())
        save_setting(db, "prog", {"x": 1}, updated_by="admin")
        from app.models.app_setting import AppSetting
        row = db.query(AppSetting).filter(AppSetting.key == "prog", AppSetting.tenant_id.is_(None)).first()
        assert row is not None
        assert row.updated_by == "admin"
        assert row.updated_at is not None


class TestWalkerProgramActions:
    """append_walker_program_action persiste e recent_walker_program_actions le."""

    def test_append_and_read_one(self):
        db = _make_session(_make_engine())
        payload = {"id": "act-1", "type": "cr_adjustment", "amount": 5, "created_at": "2026-06-11T00:00:00"}
        append_walker_program_action(db, action_type="cr", walker_id="w-1", payload=payload)
        result = recent_walker_program_actions(db, limit=20)
        assert len(result) == 1
        assert result[0]["id"] == "act-1"

    def test_append_multiple_order_chronological(self):
        """recent_walker_program_actions deve retornar em ordem cronologica (antigo->recente)."""
        import time
        db = _make_session(_make_engine())
        for i in range(3):
            payload = {"id": f"act-{i}", "type": "kit", "created_at": f"2026-06-11T0{i}:00:00"}
            append_walker_program_action(db, action_type="kit", walker_id="w-1", payload=payload)
            time.sleep(0.01)  # garante created_at diferente no SQLite
        result = recent_walker_program_actions(db, limit=20)
        assert [r["id"] for r in result] == ["act-0", "act-1", "act-2"]

    def test_limit_respected(self):
        db = _make_session(_make_engine())
        for i in range(25):
            payload = {"id": f"act-{i}", "type": "tip"}
            append_walker_program_action(db, action_type="tip", walker_id=None, payload=payload)
        result = recent_walker_program_actions(db, limit=20)
        assert len(result) == 20

    def test_empty_returns_empty_list(self):
        db = _make_session(_make_engine())
        assert recent_walker_program_actions(db) == []


# -----------------------------------------------------------------------
# Testes HTTP (camada de rota)
# -----------------------------------------------------------------------

class TestReferralProgramEndpoints:
    """GET + PUT /admin/referral-program/settings persistem no banco."""

    def setup_method(self):
        engine = _make_engine()
        self.db = _make_session(engine)
        _seed_super(self.db)
        self.client = _make_client(self.db)

    def test_get_returns_default_on_empty_db(self):
        r = self.client.get("/admin/referral-program/settings")
        assert r.status_code == 200
        body = r.json()
        assert "program_enabled" in body
        assert body["program_enabled"] is False  # default

    def test_put_persists_and_get_returns_updated(self):
        r = self.client.put(
            "/admin/referral-program/settings",
            json={"program_enabled": True},
        )
        assert r.status_code == 200
        assert r.json()["program_enabled"] is True

        # GET apos PUT deve retornar valor persistido
        r2 = self.client.get("/admin/referral-program/settings")
        assert r2.status_code == 200
        assert r2.json()["program_enabled"] is True

    def test_put_nested_partial_merge(self):
        """PUT parcial em subdict deve preservar as demais chaves do default."""
        r = self.client.put(
            "/admin/referral-program/settings",
            json={"client_rules": {"indicated_discount_amount": 999}},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["client_rules"]["indicated_discount_amount"] == 999
        # as demais chaves do client_rules default devem estar presentes
        assert "referral_limit_per_user" in body["client_rules"]

    def test_updated_at_set_on_put(self):
        r = self.client.put("/admin/referral-program/settings", json={})
        assert r.status_code == 200
        assert r.json()["updated_at"] != ""
        assert r.json()["updated_by"] == "admin"


class TestWalkerProgramEndpoints:
    """GET + PUT /admin/walker-programs/settings persistem no banco."""

    def setup_method(self):
        engine = _make_engine()
        self.db = _make_session(engine)
        _seed_super(self.db)
        self.client = _make_client(self.db)

    def test_get_returns_default_on_empty_db(self):
        r = self.client.get("/admin/walker-programs")
        assert r.status_code == 200
        body = r.json()
        assert "settings" in body
        assert "tips" in body["settings"]

    def test_put_settings_persists(self):
        r = self.client.put(
            "/admin/walker-programs/settings",
            json={"tips": {"enabled": False}},
        )
        assert r.status_code == 200
        assert r.json()["tips"]["enabled"] is False

        # GET deve refletir a mudanca
        r2 = self.client.get("/admin/walker-programs")
        assert r2.status_code == 200
        assert r2.json()["settings"]["tips"]["enabled"] is False

    def test_cr_action_persists_and_appears_in_get(self):
        """POST de acao CR deve aparecer em /admin/walker-programs['actions']."""
        r = self.client.post(
            "/admin/walker-programs/walkers/w-123/cr",
            json={"amount": 5, "reason": "Bonus"},
        )
        assert r.status_code == 200
        assert r.json()["ok"] is True

        r2 = self.client.get("/admin/walker-programs")
        assert r2.status_code == 200
        actions = r2.json()["actions"]
        assert len(actions) == 1
        assert actions[0]["type"] == "cr_adjustment"
        assert actions[0]["amount"] == 5

    def test_kit_action_persists(self):
        r = self.client.post(
            "/admin/walker-programs/walkers/w-456/kit-audit",
            json={"status": "aprovado", "note": "Tudo ok"},
        )
        assert r.status_code == 200

        r2 = self.client.get("/admin/walker-programs")
        actions = r2.json()["actions"]
        assert any(a["type"] == "kit_audit" for a in actions)

    def test_tip_review_action_persists(self):
        r = self.client.post(
            "/admin/walker-programs/tips/tip-99/review",
            json={"status": "approved", "note": "OK"},
        )
        assert r.status_code == 200

        r2 = self.client.get("/admin/walker-programs")
        actions = r2.json()["actions"]
        assert any(a["type"] == "tip_review" for a in actions)
