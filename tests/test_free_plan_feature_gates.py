"""Plano free: multiplicadores + Evolução do Pet bloqueados por PLANO.

Mapa do Carlos (2026-07-02):
  BLOQUEADO no free (independente dos toggles por tenant): recurring_plans,
  coupons, client_referrals, walker_referrals, walker_boosts, shared_walks,
  pet_tour, pet_alerts (lembretes), pet_share.
  LIBERADO no free: pet_live_profile (cadastro/ficha), walk_observations_form
  (observação do passeador no relatório), background_checks, tips, reviews,
  live_gps, push_notifications.
  Timeline/stats do pet: pro-only POR ROTA (chave pet_live_profile fica livre
  pro cadastro) → 403 teaser {"code": "plan_upgrade_required", ...}.
  Trial 21d libera TUDO (plano efetivo = pro). Pro/enterprise: zero regressão.
"""
from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.core.database import Base, get_db
from app.dependencies.auth import get_current_user
from app.models.pet import Pet
from app.models.pet_profile_config import PetProfileConfig
from app.models.tenant import Tenant, TenantFeature
from app.models.user import User
from app.services.tenant_free_plan_service import (
    FREE_PLAN_BLOCKED_FEATURE_KEYS,
    plan_blocks_feature,
)
from app.services.tenant_plan_service import (
    can_add_tenant_unit,
    enforce_plan_allows_product_feature,
    enforce_tenant_product_feature,
    get_tenant_capabilities,
    plan_allows_product_feature,
    tenant_feature_enabled,
    tenant_has_feature,
)


def _db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _tenant(db, tid, plan, **kw) -> Tenant:
    t = Tenant(id=tid, name=tid, slug=tid, status="active", plan=plan, **kw)
    db.add(t)
    db.commit()
    return t


def _trial_kw():
    return {"trial_ends_at": datetime.utcnow() + timedelta(days=10)}


# ── bloqueio por chave (tenant_feature_enabled / tenant_has_feature) ─────────

def test_blocked_keys_are_the_agreed_set():
    assert FREE_PLAN_BLOCKED_FEATURE_KEYS == {
        "recurring_plans", "coupons", "client_referrals", "walker_referrals",
        "walker_boosts", "shared_walks", "pet_tour", "pet_alerts", "pet_share",
    }


def test_free_blocks_multipliers_even_with_tenant_toggle_on():
    db = _db()
    t = _tenant(db, "t-free", "free")
    for key in FREE_PLAN_BLOCKED_FEATURE_KEYS:
        db.add(TenantFeature(tenant_id="t-free", feature_key=key, enabled=True))
    db.commit()
    for key in FREE_PLAN_BLOCKED_FEATURE_KEYS:
        assert tenant_feature_enabled(t, db, key) is False, key
        assert tenant_has_feature(t, db, key) is False, key


def test_free_keeps_allowed_features():
    db = _db()
    t = _tenant(db, "t-free", "free")
    # Default-ON continuam ON no free (não-multiplicadores).
    for key in ("tips", "reviews", "live_gps", "push_notifications",
                "protected_chat", "weekly_missions", "tutor_gamification"):
        assert tenant_feature_enabled(t, db, key) is True, key
    # Observação do passeador (relatório) e perfil/cadastro do pet: toggles
    # respeitados normalmente (a chave NÃO é bloqueada por plano).
    db.add(TenantFeature(tenant_id="t-free", feature_key="pet_live_profile", enabled=True))
    db.add(TenantFeature(tenant_id="t-free", feature_key="walk_observations_form", enabled=True))
    db.add(TenantFeature(tenant_id="t-free", feature_key="background_checks", enabled=True))
    db.commit()
    assert tenant_feature_enabled(t, db, "pet_live_profile") is True
    assert tenant_feature_enabled(t, db, "walk_observations_form") is True
    assert tenant_feature_enabled(t, db, "background_checks") is True


def test_trial_unblocks_everything():
    db = _db()
    t = _tenant(db, "t-trial", "free", **_trial_kw())
    for key in FREE_PLAN_BLOCKED_FEATURE_KEYS:
        assert plan_blocks_feature(t, key) is False, key
    # recurring_plans é default-ON → em trial volta a valer o default.
    assert tenant_feature_enabled(t, db, "recurring_plans") is True


def test_pro_enterprise_unaffected():
    db = _db()
    pro = _tenant(db, "t-pro", "pro")
    ent = _tenant(db, "t-ent", "enterprise")
    for t in (pro, ent):
        for key in FREE_PLAN_BLOCKED_FEATURE_KEYS:
            assert plan_blocks_feature(t, key) is False, (t.plan, key)
        assert tenant_feature_enabled(t, db, "recurring_plans") is True
        assert tenant_feature_enabled(t, db, "walker_boosts") is True


# ── gating de módulo por plano (plan_allows_product_feature) ─────────────────

def test_plan_gated_products_blocked_for_free():
    db = _db()
    t = _tenant(db, "t-free", "free")
    for key in ("recurring_plans", "shared_walks", "pet_tour"):
        assert plan_allows_product_feature(t, key) is False, key
        with pytest.raises(HTTPException) as exc:
            enforce_plan_allows_product_feature(t, key)
        assert exc.value.status_code == 403
        assert "plano Pro" in str(exc.value.detail)


def test_plan_gated_products_allowed_in_trial(monkeypatch):
    # Semântica v2 (produção roda PRICING_V2_ENABLED=true).
    import app.services.tenant_plan_service as tps
    monkeypatch.setattr(tps, "_PRICING_V2_ENABLED", True)
    db = _db()
    t = _tenant(db, "t-trial", "free", **_trial_kw())
    for key in ("recurring_plans", "shared_walks", "pet_tour"):
        assert plan_allows_product_feature(t, key) is True, key


def test_plan_gated_products_unchanged_for_existing_plans(monkeypatch):
    import app.services.tenant_plan_service as tps
    db = _db()
    biz = _tenant(db, "t-biz", "business")
    ent = _tenant(db, "t-ent", "enterprise")
    # v1 (flag OFF — default): business/enterprise seguem liberados.
    assert plan_allows_product_feature(biz, "recurring_plans") is True
    assert plan_allows_product_feature(ent, "pet_tour") is True
    # v2 (flag ON): pro/enterprise liberados.
    monkeypatch.setattr(tps, "_PRICING_V2_ENABLED", True)
    pro = _tenant(db, "t-pro", "pro")
    assert plan_allows_product_feature(pro, "shared_walks") is True
    assert plan_allows_product_feature(ent, "recurring_plans") is True


# ── teaser 403 no gate de produto (cupons/pet_tour/shared no free) ───────────

def test_enforce_product_feature_free_returns_teaser_shape():
    db = _db()
    t = _tenant(db, "t-free", "free")
    db.add(TenantFeature(tenant_id="t-free", feature_key="coupons", enabled=True))
    db.commit()
    with pytest.raises(HTTPException) as exc:
        enforce_tenant_product_feature(t, db, "coupons", "Cupons")
    assert exc.value.status_code == 403
    detail = exc.value.detail
    # Shape do teaser (contrato admin-web/app): code + required_plan + feature + message.
    assert detail["code"] == "plan_upgrade_required"
    assert detail["required_plan"] == "pro"
    assert detail["feature"] == "coupons"
    assert "plano Pro" in detail["message"]


# ── capabilities / multi-unidade ─────────────────────────────────────────────

def test_free_capabilities_minimal():
    db = _db()
    t = _tenant(db, "t-free", "free")
    caps = get_tenant_capabilities(t, db)
    assert caps["max_units"] == 1
    assert caps["max_units_with_addon"] == 1
    assert caps["network_access_available"] is False
    assert caps["dedicated_app_allowed"] is False
    assert caps["custom_products_allowed"] is False


def test_free_trial_capabilities_are_pro(monkeypatch):
    import app.services.tenant_plan_service as tps
    monkeypatch.setattr(tps, "_PRICING_V2_ENABLED", True)
    db = _db()
    t = _tenant(db, "t-trial", "free", **_trial_kw())
    caps = get_tenant_capabilities(t, db)
    assert caps["max_units"] == 2                       # Pro
    assert caps["network_access_available"] is True     # Pro


def test_free_multi_unit_blocked_after_first(db=None):
    from app.models.tenant import TenantUnit
    db = _db()
    t = _tenant(db, "t-free", "free")
    assert can_add_tenant_unit(t, db) is True   # 0 < 1
    db.add(TenantUnit(tenant_id="t-free", name="Matriz"))
    db.commit()
    assert can_add_tenant_unit(t, db) is False  # 1 >= 1


def test_pro_enterprise_units_unchanged():
    db = _db()
    pro = _tenant(db, "t-pro", "pro")
    ent = _tenant(db, "t-ent", "enterprise")
    assert can_add_tenant_unit(pro, db) is True
    assert can_add_tenant_unit(ent, db) is True


# ── Evolução do Pet: timeline/stats pro-only por rota (teaser) ───────────────

def _pet_ctx(plan="free", **tenant_kw):
    db = _db()
    _tenant(db, "t1", plan, **tenant_kw)
    db.add(User(id="u1", email="u1@x.com", password_hash="x", role="tutor", tenant_id="t1"))
    db.add(Pet(id="p1", tutor_id="u1", tenant_id="t1", name="Rex"))
    # 3 camadas do pet_live_profile TODAS ligadas — o bloqueio deve vir do PLANO.
    db.add(TenantFeature(tenant_id="t1", feature_key="pet_live_profile", enabled=True))
    db.add(PetProfileConfig(tenant_id="t1", profile_enabled=True))
    db.commit()
    return db


def _pet_client(db, monkeypatch):
    from app.routes import pet_diary_routes  # noqa: F401 — anexa rotas Fase B/5
    from app.routes import pet_profile as routes
    monkeypatch.setenv("PET_LIVE_PROFILE_ENABLED", "true")
    app = FastAPI()
    app.include_router(routes.api_router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: db.get(User, "u1")
    return TestClient(app)


def test_free_timeline_returns_teaser_403(monkeypatch):
    db = _pet_ctx()
    c = _pet_client(db, monkeypatch)
    r = c.get("/api/pets/p1/timeline")
    assert r.status_code == 403
    body = r.json()["detail"]
    assert body["code"] == "plan_upgrade_required"
    assert body["feature"] == "pet_timeline"
    assert body["required_plan"] == "pro"
    # POST e DELETE também bloqueiam
    r2 = c.post("/api/pets/p1/timeline", json={
        "event_type": "weight", "title": "Peso", "occurred_at": "2026-07-01T00:00:00"})
    assert r2.status_code == 403


def test_free_stats_returns_teaser_403(monkeypatch):
    db = _pet_ctx()
    c = _pet_client(db, monkeypatch)
    r = c.get("/api/pets/p1/stats")
    assert r.status_code == 403
    assert r.json()["detail"]["feature"] == "pet_stats"


def test_free_pet_health_patch_still_allowed(monkeypatch):
    # Cadastro/ficha do pet LIBERADO no free (mapa do Carlos).
    db = _pet_ctx()
    c = _pet_client(db, monkeypatch)
    r = c.patch("/api/pets/p1/profile", json={"weight": 12.5})
    assert r.status_code == 200


def test_trial_timeline_allowed(monkeypatch):
    db = _pet_ctx(trial_ends_at=datetime.utcnow() + timedelta(days=5))
    c = _pet_client(db, monkeypatch)
    assert c.get("/api/pets/p1/timeline").status_code == 200
    assert c.get("/api/pets/p1/stats").status_code == 200


def test_pro_timeline_unchanged(monkeypatch):
    db = _pet_ctx(plan="pro")
    c = _pet_client(db, monkeypatch)
    assert c.get("/api/pets/p1/timeline").status_code == 200
    assert c.get("/api/pets/p1/stats").status_code == 200


def test_free_reminders_sweep_skips_tenant(monkeypatch):
    # pet_alerts bloqueado por plano → reminders_active False → sweep pula o tenant.
    monkeypatch.setenv("PET_ALERTS_ENABLED", "true")
    db = _db()
    t = _tenant(db, "t-free", "free")
    db.add(TenantFeature(tenant_id="t-free", feature_key="pet_alerts", enabled=True))
    db.add(PetProfileConfig(tenant_id="t-free", reminders_enabled=True))
    db.commit()
    from app.services.pet_profile_service import reminders_active
    assert reminders_active(t, db) is False


# ── pets por tutor (máx 2 no free) ───────────────────────────────────────────

def test_free_pet_limit_blocks_third_pet(monkeypatch):
    from app.services.tenant_free_plan_service import enforce_free_plan_pet_limit
    monkeypatch.delenv("FREE_PLAN_PETS_PER_TUTOR", raising=False)
    db = _db()
    t = _tenant(db, "t-free", "free")
    db.add(Pet(id="p1", tutor_id="u1", tenant_id="t-free", name="Rex"))
    db.add(Pet(id="p2", tutor_id="u1", tenant_id="t-free", name="Bob"))
    db.commit()
    with pytest.raises(HTTPException) as exc:
        enforce_free_plan_pet_limit(db, t, "u1")
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "plan_upgrade_required"
    assert exc.value.detail["feature"] == "pets_per_tutor"


def test_free_pet_limit_allows_below_and_env(monkeypatch):
    from app.services.tenant_free_plan_service import enforce_free_plan_pet_limit
    db = _db()
    t = _tenant(db, "t-free", "free")
    db.add(Pet(id="p1", tutor_id="u1", tenant_id="t-free", name="Rex"))
    db.commit()
    enforce_free_plan_pet_limit(db, t, "u1")  # 1 < 2 → ok
    monkeypatch.setenv("FREE_PLAN_PETS_PER_TUTOR", "1")
    with pytest.raises(HTTPException):
        enforce_free_plan_pet_limit(db, t, "u1")  # 1 >= 1 → bloqueia


def test_pet_limit_excess_kept_after_downgrade_but_no_new(monkeypatch):
    # Downgrade NÃO remove excedentes: tutor com 3 pets (criados no trial) mantém
    # os 3; apenas não cria o 4º.
    from app.services.tenant_free_plan_service import enforce_free_plan_pet_limit
    monkeypatch.delenv("FREE_PLAN_PETS_PER_TUTOR", raising=False)
    db = _db()
    t = _tenant(db, "t-free", "free", trial_ends_at=datetime.utcnow() - timedelta(days=1))
    for i in range(3):
        db.add(Pet(id=f"p{i}", tutor_id="u1", tenant_id="t-free", name=f"Pet{i}"))
    db.commit()
    assert db.query(Pet).filter(Pet.tutor_id == "u1").count() == 3  # mantidos
    with pytest.raises(HTTPException):
        enforce_free_plan_pet_limit(db, t, "u1")  # novo pet bloqueado


def test_pet_limit_ignores_pro_and_trial():
    from app.services.tenant_free_plan_service import enforce_free_plan_pet_limit
    db = _db()
    pro = _tenant(db, "t-pro", "pro")
    trial = _tenant(db, "t-trial", "free", **_trial_kw())
    for i in range(5):
        db.add(Pet(id=f"pp{i}", tutor_id="u9", tenant_id="t-pro", name=f"P{i}"))
    db.commit()
    enforce_free_plan_pet_limit(db, pro, "u9")    # no-op
    enforce_free_plan_pet_limit(db, trial, "u9")  # trial → no-op
