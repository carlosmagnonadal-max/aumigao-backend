"""Nudge de upgrade: GET /admin/tenants/{id}/plan-usage + sweep interno do trial.

SHAPE JSON (contrato pro admin-web — ver build_plan_usage):
  tenant_id · plan · effective_plan · trial{active, ends_at, days_left,
  downgraded_at} · period ("YYYY-MM" BRT) · walks{used, cap, remaining} ·
  limits{pets_per_tutor} · commission{percent, month_total, gmv_month} ·
  pro_projection{monthly_fee, commission_percent, commission_month, total_month,
  savings_month} · upgrade_recommended.
"""
from datetime import datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.core.database import Base, get_db
from app.dependencies.auth import get_current_user
from app.models.commission_entry import COMM_ACCRUED, COMM_VOID, CommissionEntry
from app.models.notification import Notification
from app.models.tenant import Tenant
from app.models.user import User
from app.models.walk import Walk
from app.routes import tenants as tenants_routes
from app.services.tenant_free_plan_service import current_month_window_utc


def _db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _client(db, user_id="sa"):
    app = FastAPI()
    app.include_router(tenants_routes.router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: db.get(User, user_id)
    return TestClient(app)


def _seed(db, plan="free", **tenant_kw):
    db.add(User(id="sa", email="sa@x.com", password_hash="x", role="super_admin"))
    t = Tenant(id="t1", name="t1", slug="t1", status="active", plan=plan, **tenant_kw)
    db.add(t)
    db.commit()
    return t


def _walk(db, i, status="Agendado"):
    db.add(Walk(id=f"w{i}", tutor_id="tu", tenant_id="t1", pet_id="p1",
                scheduled_date="2099-01-01T10:00:00", duration_minutes=30,
                price=30.0, status=status, created_at=datetime.utcnow()))
    db.commit()


def _entry(db, i, amount, walk_price, status=COMM_ACCRUED):
    _, period = current_month_window_utc()
    db.add(CommissionEntry(id=f"ce{i}", tenant_id="t1", walk_id=f"cw{i}", period=period,
                           walk_price=walk_price, commission_percent=20.0,
                           amount=amount, is_network=False, status=status))
    db.commit()


def test_plan_usage_shape_and_math_for_free(monkeypatch):
    monkeypatch.setenv("FREE_PLAN_WALK_CAP", "40")
    monkeypatch.delenv("FREE_PLAN_PETS_PER_TUTOR", raising=False)
    db = _db()
    _seed(db)
    _walk(db, 1)
    _walk(db, 2)
    _walk(db, 3, status="Cancelado")  # não conta
    # Comissão medida no mês: R$168 de comissão sobre GMV R$840 (20%).
    _entry(db, 1, 100.0, 500.0)
    _entry(db, 2, 68.0, 340.0)
    _entry(db, 3, 999.0, 999.0, status=COMM_VOID)  # excluída

    r = _client(db).get("/admin/tenants/t1/plan-usage")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tenant_id"] == "t1"
    assert body["plan"] == "free"
    assert body["effective_plan"] == "free"
    assert body["trial"] == {"active": False, "ends_at": None, "days_left": None, "downgraded_at": None}
    assert body["walks"] == {"used": 2, "cap": 40, "remaining": 38}
    assert body["limits"] == {"pets_per_tutor": 2}
    assert body["commission"]["percent"] == 20.0
    assert body["commission"]["month_total"] == 168.0
    assert body["commission"]["gmv_month"] == 840.0
    proj = body["pro_projection"]
    assert proj["monthly_fee"] == 129.90
    assert proj["commission_percent"] == 10.0
    assert proj["commission_month"] == 84.0          # 10% × 840
    assert proj["total_month"] == 213.90             # 129,90 + 84
    assert proj["savings_month"] == -45.90           # 168 − 213,90 (Pro ainda não compensa)
    assert body["upgrade_recommended"] is False


def test_plan_usage_recommends_upgrade_when_pro_cheaper(monkeypatch):
    monkeypatch.setenv("FREE_PLAN_WALK_CAP", "40")
    db = _db()
    _seed(db)
    # GMV alto: 20% = 400 > 129,90 + 10%×2000 = 329,90 → Pro compensa.
    _entry(db, 1, 400.0, 2000.0)
    body = _client(db).get("/admin/tenants/t1/plan-usage").json()
    assert body["pro_projection"]["savings_month"] == 70.10
    assert body["upgrade_recommended"] is True


def test_plan_usage_recommends_upgrade_at_cap(monkeypatch):
    monkeypatch.setenv("FREE_PLAN_WALK_CAP", "2")
    db = _db()
    _seed(db)
    _walk(db, 1)
    _walk(db, 2)
    body = _client(db).get("/admin/tenants/t1/plan-usage").json()
    assert body["walks"] == {"used": 2, "cap": 2, "remaining": 0}
    assert body["upgrade_recommended"] is True


def test_plan_usage_trial_fields(monkeypatch):
    db = _db()
    ends = datetime.utcnow() + timedelta(days=10, hours=1)
    _seed(db, trial_ends_at=ends)
    body = _client(db).get("/admin/tenants/t1/plan-usage").json()
    assert body["plan"] == "free"
    assert body["effective_plan"] == "pro"
    assert body["trial"]["active"] is True
    assert body["trial"]["days_left"] == 10
    assert body["walks"]["cap"] is None       # sem cap durante o trial
    assert body["limits"]["pets_per_tutor"] is None
    assert body["commission"]["percent"] == 10.0  # comissão Pro no trial


def test_plan_usage_pro_tenant_no_caps():
    db = _db()
    _seed(db, plan="pro")
    body = _client(db).get("/admin/tenants/t1/plan-usage").json()
    assert body["plan"] == "pro"
    assert body["effective_plan"] == "pro"
    assert body["walks"]["cap"] is None
    assert body["limits"]["pets_per_tutor"] is None
    assert body["upgrade_recommended"] is False


def test_plan_usage_triggers_lazy_downgrade():
    db = _db()
    t = _seed(db, trial_ends_at=datetime.utcnow() - timedelta(days=1))
    db.add(User(id="adm", email="a@x.com", password_hash="x", role="admin",
                tenant_id="t1", is_active=True))
    db.commit()
    body = _client(db).get("/admin/tenants/t1/plan-usage").json()
    db.refresh(t)
    assert t.trial_downgraded_at is not None  # carimbou lazy no request
    assert body["trial"]["downgraded_at"] is not None
    assert body["effective_plan"] == "free"
    assert db.query(Notification).filter(Notification.user_id == "adm").count() == 1


def test_plan_usage_scope_cross_tenant_404():
    db = _db()
    _seed(db)
    db.add(Tenant(id="t2", name="t2", slug="t2", status="active", plan="pro"))
    db.add(User(id="adm2", email="a2@x.com", password_hash="x", role="admin", tenant_id="t2"))
    db.commit()
    r = _client(db, user_id="adm2").get("/admin/tenants/t1/plan-usage")
    # Sem RBAC tenants.read → 403 na camada do router; com a permissão, o
    # _scope_or_404 devolveria 404 cross-tenant. Ambos NÃO vazam dados.
    assert r.status_code in (403, 404)
    assert "t1" not in r.text


def test_internal_free_trial_sweep(monkeypatch):
    from app.routes import payments as payments_routes
    from app.core.database import get_global_db

    monkeypatch.setenv("INTERNAL_SWEEP_TOKEN", "tok-123")
    db = _db()
    _seed(db, trial_ends_at=datetime.utcnow() - timedelta(days=2))
    db.add(User(id="adm", email="a@x.com", password_hash="x", role="admin",
                tenant_id="t1", is_active=True))
    db.commit()

    app = FastAPI()
    app.include_router(payments_routes.router)
    app.dependency_overrides[get_global_db] = lambda: db
    c = TestClient(app)

    # Sem token → 401.
    assert c.post("/payments/internal/free-trial/sweep").status_code == 401
    # Com token → carimba 1.
    r = c.post("/payments/internal/free-trial/sweep", headers={"x-internal-token": "tok-123"})
    assert r.status_code == 200
    assert r.json() == {"downgraded": 1}
    # Idempotente.
    r2 = c.post("/payments/internal/free-trial/sweep", headers={"x-internal-token": "tok-123"})
    assert r2.json() == {"downgraded": 0}
