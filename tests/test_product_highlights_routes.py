"""Testes da Vitrine de Destaques e Promoções (Fase 1) — CRUD, limite, gating, RLS/escopo.

Gating em 3 camadas (env master + toggle por tenant + plano ENTERPRISE-only):
  - dormante (env off / toggle off) → 404
  - toggle on mas plano free/pro → 403 plan_upgrade_required (required_plan="enterprise")
  - toggle on + enterprise → 200
"""
from __future__ import annotations

import app.models  # noqa: F401

from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.dependencies.auth import get_current_user
from app.models.tenant import Tenant, TenantFeature
from app.models.tenant_product_highlight import TenantProductHighlight
from app.models.user import User
from app.routes import product_highlights as routes


def _ctx(*, plan="enterprise", toggle=True):
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    db.add(Tenant(id="t1", name="T1", slug="t1", status="active", plan=plan))
    db.add(Tenant(id="t2", name="T2", slug="t2", status="active", plan="enterprise"))
    db.add(User(id="adm1", email="a1@x.com", password_hash="x", role="admin", tenant_id="t1"))
    db.add(User(id="adm2", email="a2@x.com", password_hash="x", role="admin", tenant_id="t2"))
    db.add(User(id="tut1", email="t1u@x.com", password_hash="x", role="tutor", tenant_id="t1"))
    if toggle:
        db.add(TenantFeature(tenant_id="t1", feature_key="product_highlights", enabled=True))
        db.add(TenantFeature(tenant_id="t2", feature_key="product_highlights", enabled=True))
    db.commit()
    return db


def _client(db, user, env, monkeypatch):
    if env:
        monkeypatch.setenv("PRODUCT_HIGHLIGHTS_ENABLED", "true")
    else:
        monkeypatch.delenv("PRODUCT_HIGHLIGHTS_ENABLED", raising=False)
    # PRICING_V2 ON: enterprise canônico resolvido corretamente pelo gate de plano.
    monkeypatch.setenv("PRICING_V2_ENABLED", "true")
    app = FastAPI()
    app.include_router(routes.api_router)
    app.include_router(routes.api_tutor_router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app)


def _seed(db, tenant_id, n, *, active=True, prefix="P"):
    now = datetime.utcnow()
    for i in range(n):
        db.add(TenantProductHighlight(
            id=f"{tenant_id}-{prefix}-{i}", tenant_id=tenant_id, title=f"{prefix}{i}",
            is_active=active, sort_order=i, created_at=now, updated_at=now,
        ))
    db.commit()


# ---------------------------------------------------------------------------
# Gating
# ---------------------------------------------------------------------------

def test_env_off_returns_404(monkeypatch):
    db = _ctx()
    c = _client(db, db.get(User, "adm1"), env=False, monkeypatch=monkeypatch)
    assert c.get("/api/admin/product-highlights").status_code == 404


def test_toggle_off_returns_404(monkeypatch):
    db = _ctx(toggle=False)
    c = _client(db, db.get(User, "adm1"), env=True, monkeypatch=monkeypatch)
    assert c.get("/api/admin/product-highlights").status_code == 404


def test_free_plan_returns_403_enterprise_teaser(monkeypatch):
    db = _ctx(plan="free")
    c = _client(db, db.get(User, "adm1"), env=True, monkeypatch=monkeypatch)
    r = c.get("/api/admin/product-highlights")
    assert r.status_code == 403
    body = r.json()["detail"]
    assert body["code"] == "plan_upgrade_required"
    assert body["required_plan"] == "enterprise"


def test_pro_plan_returns_403_enterprise_teaser(monkeypatch):
    db = _ctx(plan="pro")
    c = _client(db, db.get(User, "adm1"), env=True, monkeypatch=monkeypatch)
    r = c.get("/api/admin/product-highlights")
    assert r.status_code == 403
    assert r.json()["detail"]["required_plan"] == "enterprise"


def test_enterprise_toggle_on_returns_200(monkeypatch):
    db = _ctx(plan="enterprise")
    c = _client(db, db.get(User, "adm1"), env=True, monkeypatch=monkeypatch)
    r = c.get("/api/admin/product-highlights")
    assert r.status_code == 200
    assert r.json()["items"] == []
    assert r.json()["max_active"] == 6


# ---------------------------------------------------------------------------
# CRUD feliz
# ---------------------------------------------------------------------------

def test_crud_happy_path(monkeypatch):
    db = _ctx()
    c = _client(db, db.get(User, "adm1"), env=True, monkeypatch=monkeypatch)
    # create
    r = c.post("/api/admin/product-highlights", json={
        "title": "  Banho e Tosa  ", "description": "Promo de julho",
        "price_cents": 9000, "promo_price_cents": 6900, "sort_order": 1,
    })
    assert r.status_code == 201
    item = r.json()["item"]
    assert item["title"] == "Banho e Tosa"  # trim
    assert item["is_active"] is True
    hid = item["id"]
    # list
    assert len(c.get("/api/admin/product-highlights").json()["items"]) == 1
    # patch
    r2 = c.patch(f"/api/admin/product-highlights/{hid}", json={"title": "Banho Premium"})
    assert r2.status_code == 200 and r2.json()["item"]["title"] == "Banho Premium"
    # delete
    assert c.delete(f"/api/admin/product-highlights/{hid}").status_code == 200
    assert c.get("/api/admin/product-highlights").json()["items"] == []


def test_patch_deactivate_then_reactivate_respects_limit(monkeypatch):
    db = _ctx()
    _seed(db, "t1", 6, active=True)  # exatamente no limite
    c = _client(db, db.get(User, "adm1"), env=True, monkeypatch=monkeypatch)
    # desativa um → agora 5 ativos
    assert c.patch("/api/admin/product-highlights/t1-P-0", json={"is_active": False}).status_code == 200
    # reativa o mesmo → volta a 6 (permitido, limite é 6)
    assert c.patch("/api/admin/product-highlights/t1-P-0", json={"is_active": True}).status_code == 200


# ---------------------------------------------------------------------------
# Limite de ativos
# ---------------------------------------------------------------------------

def test_create_over_active_limit_returns_422(monkeypatch):
    db = _ctx()
    _seed(db, "t1", 6, active=True)
    c = _client(db, db.get(User, "adm1"), env=True, monkeypatch=monkeypatch)
    r = c.post("/api/admin/product-highlights", json={"title": "Excedente"})
    assert r.status_code == 422
    assert "6" in r.json()["detail"]


def test_create_inactive_over_limit_is_allowed(monkeypatch):
    db = _ctx()
    _seed(db, "t1", 6, active=True)
    c = _client(db, db.get(User, "adm1"), env=True, monkeypatch=monkeypatch)
    # criar INATIVO não conta no limite de ativos
    r = c.post("/api/admin/product-highlights", json={"title": "Rascunho", "is_active": False})
    assert r.status_code == 201


def test_limit_configurable_via_env(monkeypatch):
    monkeypatch.setenv("PRODUCT_HIGHLIGHTS_MAX_ACTIVE", "2")
    db = _ctx()
    _seed(db, "t1", 2, active=True)
    c = _client(db, db.get(User, "adm1"), env=True, monkeypatch=monkeypatch)
    assert c.post("/api/admin/product-highlights", json={"title": "3o"}).status_code == 422


# ---------------------------------------------------------------------------
# Validação de preço
# ---------------------------------------------------------------------------

def test_promo_ge_price_returns_422(monkeypatch):
    db = _ctx()
    c = _client(db, db.get(User, "adm1"), env=True, monkeypatch=monkeypatch)
    r = c.post("/api/admin/product-highlights", json={
        "title": "X", "price_cents": 5000, "promo_price_cents": 5000})
    assert r.status_code == 422
    r2 = c.post("/api/admin/product-highlights", json={
        "title": "X", "price_cents": 5000, "promo_price_cents": 6000})
    assert r2.status_code == 422


def test_patch_promo_ge_price_returns_422(monkeypatch):
    db = _ctx()
    c = _client(db, db.get(User, "adm1"), env=True, monkeypatch=monkeypatch)
    hid = c.post("/api/admin/product-highlights", json={
        "title": "X", "price_cents": 5000, "promo_price_cents": 3000}).json()["item"]["id"]
    # só muda promo para >= price existente
    r = c.patch(f"/api/admin/product-highlights/{hid}", json={"promo_price_cents": 9000})
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# RLS / escopo (admin de outro tenant não vê/escreve)
# ---------------------------------------------------------------------------

def test_admin_scope_isolation_read(monkeypatch):
    db = _ctx()
    _seed(db, "t2", 2, active=True)  # itens do tenant t2
    c = _client(db, db.get(User, "adm1"), env=True, monkeypatch=monkeypatch)  # admin do t1
    # admin do t1 NÃO vê itens do t2
    assert c.get("/api/admin/product-highlights").json()["items"] == []


def test_admin_scope_isolation_write(monkeypatch):
    db = _ctx()
    _seed(db, "t2", 1, active=True, prefix="X")  # item do t2
    c = _client(db, db.get(User, "adm1"), env=True, monkeypatch=monkeypatch)  # admin do t1
    # admin do t1 não consegue editar/deletar item do t2 → 404 (não vaza existência)
    assert c.patch("/api/admin/product-highlights/t2-X-0", json={"title": "hack"}).status_code == 404
    assert c.delete("/api/admin/product-highlights/t2-X-0").status_code == 404


# ---------------------------------------------------------------------------
# Rota pública do tutor
# ---------------------------------------------------------------------------

def test_tutor_route_returns_only_active_of_own_tenant(monkeypatch):
    db = _ctx()
    _seed(db, "t1", 2, active=True, prefix="A")
    _seed(db, "t1", 1, active=False, prefix="I")   # inativo — não deve aparecer
    _seed(db, "t2", 3, active=True, prefix="B")     # outro tenant — não deve aparecer
    c = _client(db, db.get(User, "tut1"), env=True, monkeypatch=monkeypatch)
    r = c.get("/api/product-highlights")
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 2
    assert all(i["id"].startswith("t1-A-") for i in items)
    # ordenados por sort_order
    assert [i["sort_order"] for i in items] == [0, 1]
    # não expõe is_active; expõe has_promo/effective_price_cents
    assert "is_active" not in items[0]
    assert "has_promo" in items[0]


def test_tutor_route_gated_by_plan(monkeypatch):
    db = _ctx(plan="free")
    c = _client(db, db.get(User, "tut1"), env=True, monkeypatch=monkeypatch)
    r = c.get("/api/product-highlights")
    assert r.status_code == 403
    assert r.json()["detail"]["required_plan"] == "enterprise"


def test_tutor_route_public_promo_fields(monkeypatch):
    db = _ctx()
    c_admin = _client(db, db.get(User, "adm1"), env=True, monkeypatch=monkeypatch)
    c_admin.post("/api/admin/product-highlights", json={
        "title": "Combo", "price_cents": 10000, "promo_price_cents": 7500})
    c_tut = _client(db, db.get(User, "tut1"), env=True, monkeypatch=monkeypatch)
    item = c_tut.get("/api/product-highlights").json()["items"][0]
    assert item["has_promo"] is True
    assert item["effective_price_cents"] == 7500
