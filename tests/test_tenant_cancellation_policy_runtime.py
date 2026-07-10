"""Mig 0107 — cancellation_policy no payload de features-runtime que o app tutor
ja consome (GET /tenants/current/features-runtime). Substitui o hardcode
CANCELLATION_FREE_HOURS=24/LATE_CANCELLATION_FEE_PERCENT=50 de
frontend/constants/cancellationPolicy.ts. Reusa o fixture `build()` de
tests/test_routes_tenant_features_runtime.py.
"""
from uuid import uuid4

from app.models.tenant import Tenant, TenantSettings
from tests.test_routes_tenant_features_runtime import DEFAULT_TENANT_ID, build


def test_current_runtime_exposes_factory_default_policy():
    client, db = build()
    r = client.get("/tenants/current/features-runtime")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["cancellation_policy"] == {
        "free_window_minutes": 1440,
        "late_fee_percent": 50,
        "auto_refund_enabled": True,
    }


def test_current_runtime_exposes_custom_tenant_policy():
    client, db = build()
    db.add(TenantSettings(
        id=str(uuid4()), tenant_id=DEFAULT_TENANT_ID, timezone="America/Bahia",
        cancellation_free_window_minutes=720, late_cancellation_fee_percent=30,
        late_fee_walker_share_percent=50, auto_refund_on_cancel=False,
    ))
    db.commit()

    r = client.get("/tenants/current/features-runtime")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["cancellation_policy"] == {
        "free_window_minutes": 720,
        "late_fee_percent": 30,
        "auto_refund_enabled": False,
    }


def test_by_id_runtime_also_exposes_cancellation_policy():
    client, db = build()
    r = client.get(f"/tenants/{DEFAULT_TENANT_ID}/features-runtime")
    assert r.status_code == 200, r.text
    assert r.json()["cancellation_policy"]["free_window_minutes"] == 1440
