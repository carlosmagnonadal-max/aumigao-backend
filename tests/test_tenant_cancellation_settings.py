"""Mig 0107 — config de cancelamento por tenant no GET/PATCH
/admin/tenants/{id}/settings (pagina master de tenants do admin-web). Mesmo
padrao do meeting_point_discount: defaults de fabrica + validacao de escrita
(janela > 0, percentuais 0-100).
"""
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.core.database import Base, get_db
from app.dependencies.auth import get_current_user
from app.models.tenant import Tenant, TenantSettings
from app.models.user import User
from app.routes import tenants

ADMIN_ID = "admin-test"


def build():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(User(id=ADMIN_ID, email="admin@test.com", password_hash="x", role="super_admin"))
    db.add(Tenant(id="t-1", name="Alpha", slug="alpha", status="active", plan="business"))
    db.commit()
    test_app = FastAPI()
    test_app.include_router(tenants.router)
    test_app.dependency_overrides[get_db] = lambda: db
    test_app.dependency_overrides[get_current_user] = lambda: db.get(User, ADMIN_ID)
    return TestClient(test_app), db


def test_get_settings_without_row_returns_factory_defaults():
    client, db = build()
    r = client.get("/admin/tenants/t-1/settings")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["cancellation_free_window_minutes"] == 1440
    assert body["late_cancellation_fee_percent"] == 50
    assert body["late_fee_walker_share_percent"] == 100
    assert body["auto_refund_on_cancel"] is True


def test_patch_settings_updates_cancellation_config():
    client, db = build()
    r = client.patch("/admin/tenants/t-1/settings", json={
        "cancellation_free_window_minutes": 720,
        "late_cancellation_fee_percent": 30,
        "late_fee_walker_share_percent": 50,
        "auto_refund_on_cancel": False,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["cancellation_free_window_minutes"] == 720
    assert body["late_cancellation_fee_percent"] == 30
    assert body["late_fee_walker_share_percent"] == 50
    assert body["auto_refund_on_cancel"] is False

    db.expire_all()
    settings = db.query(TenantSettings).filter(TenantSettings.tenant_id == "t-1").first()
    assert settings.cancellation_free_window_minutes == 720
    assert settings.late_cancellation_fee_percent == 30
    assert settings.late_fee_walker_share_percent == 50
    assert settings.auto_refund_on_cancel is False


def test_patch_settings_partial_update_preserves_other_cancellation_fields():
    client, db = build()
    client.patch("/admin/tenants/t-1/settings", json={"late_cancellation_fee_percent": 25})
    r = client.get("/admin/tenants/t-1/settings")
    body = r.json()
    assert body["late_cancellation_fee_percent"] == 25
    # Campos nao enviados mantem o default de fabrica.
    assert body["cancellation_free_window_minutes"] == 1440
    assert body["late_fee_walker_share_percent"] == 100
    assert body["auto_refund_on_cancel"] is True


def test_patch_settings_rejects_zero_window():
    client, db = build()
    r = client.patch("/admin/tenants/t-1/settings", json={"cancellation_free_window_minutes": 0})
    assert r.status_code == 422


def test_patch_settings_rejects_negative_window():
    client, db = build()
    r = client.patch("/admin/tenants/t-1/settings", json={"cancellation_free_window_minutes": -10})
    assert r.status_code == 422


def test_patch_settings_rejects_fee_percent_above_100():
    client, db = build()
    r = client.patch("/admin/tenants/t-1/settings", json={"late_cancellation_fee_percent": 101})
    assert r.status_code == 422


def test_patch_settings_rejects_fee_percent_below_zero():
    client, db = build()
    r = client.patch("/admin/tenants/t-1/settings", json={"late_cancellation_fee_percent": -1})
    assert r.status_code == 422


def test_patch_settings_rejects_walker_share_out_of_bounds():
    client, db = build()
    r = client.patch("/admin/tenants/t-1/settings", json={"late_fee_walker_share_percent": 150})
    assert r.status_code == 422


def test_patch_settings_accepts_boundary_values():
    client, db = build()
    r = client.patch("/admin/tenants/t-1/settings", json={
        "late_cancellation_fee_percent": 0,
        "late_fee_walker_share_percent": 100,
        "cancellation_free_window_minutes": 1,
    })
    assert r.status_code == 200, r.text
