"""TDD — relatório de saúde do pool por tenant (F3.3)."""
from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.models.user import User
from app.models.tenant import Tenant
from app.models.tenant_walker_access import TenantWalkerAccess
from app.models.walk import Walk
from app.models.payment import Payment
from app.services.tenant_report_service import build_tenant_pool_report, _completion_rate


def _db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


NOW = datetime(2026, 6, 23, 12, 0)
FROM = NOW - timedelta(days=30)
TO = NOW


def _walk(db, tid, wid, status, created, walk_id=None):
    walk_id = walk_id or str(uuid4())
    db.add(Walk(id=walk_id, tenant_id=tid, walker_id=wid, operational_status=status,
                tutor_id="tutor1", pet_id="pet1", scheduled_date="2026-06-20",
                duration_minutes=45, price=30.0, created_at=created))
    db.commit()
    return walk_id


def _pay(db, tid, walk_id, amount):
    db.add(Payment(id=str(uuid4()), tenant_id=tid, tutor_id="tutor1", walk_id=walk_id,
                   amount=amount, walker_amount=amount, status="paid"))
    db.commit()


def _walker(db, wid):
    db.add(User(id=wid, email=f"{wid}@i", password_hash="x", role="walker", is_active=True,
                token_version=0, must_change_password=False))
    db.commit()


def test_completion_rate_helper():
    assert _completion_rate(3, 4) == 0.75
    assert _completion_rate(0, 0) == 0.0


def test_report_conta_completed_cancelled_e_taxa():
    db = _db()
    db.add(Tenant(id="tA", name="A", slug="a")); db.commit()
    _walker(db, "w1")
    _walk(db, "tA", "w1", "ride_completed", NOW - timedelta(days=1))
    _walk(db, "tA", "w1", "ride_completed", NOW - timedelta(days=2))
    _walk(db, "tA", "w1", "ride_cancelled", NOW - timedelta(days=3))
    _walk(db, "tA", "w1", "ride_completed", NOW - timedelta(days=60))  # fora do período
    _walk(db, "tB", "w1", "ride_completed", NOW - timedelta(days=1))   # outro tenant

    rep = build_tenant_pool_report(db, "tA", FROM, TO)
    assert rep["walks"]["completed"] == 2
    assert rep["walks"]["cancelled"] == 1
    assert rep["walks"]["total"] == 3
    assert rep["walks"]["completion_rate"] == round(2 / 3, 2)


def test_report_active_walkers_revenue_e_top():
    db = _db()
    db.add(Tenant(id="tA", name="A", slug="a")); db.commit()
    _walker(db, "w1"); _walker(db, "w2")
    db.add(TenantWalkerAccess(id=str(uuid4()), tenant_id="tA", walker_user_id="w1",
                              status="active", access_type="shared_network"))
    db.add(TenantWalkerAccess(id=str(uuid4()), tenant_id="tA", walker_user_id="w2",
                              status="active", access_type="shared_network"))
    db.commit()
    k1 = _walk(db, "tA", "w1", "ride_completed", NOW - timedelta(days=1))
    k2 = _walk(db, "tA", "w1", "ride_completed", NOW - timedelta(days=2))
    k3 = _walk(db, "tA", "w2", "ride_completed", NOW - timedelta(days=1))
    _pay(db, "tA", k1, 30.0); _pay(db, "tA", k2, 20.0); _pay(db, "tA", k3, 25.0)

    rep = build_tenant_pool_report(db, "tA", FROM, TO)
    assert rep["walkers"]["active"] == 2
    assert rep["revenue"]["walker_amount_total"] == 75.0
    assert rep["top_walkers"][0]["walker_user_id"] == "w1"
    assert rep["top_walkers"][0]["completed_walks"] == 2


def test_report_vazio_zera():
    db = _db()
    db.add(Tenant(id="tA", name="A", slug="a")); db.commit()
    rep = build_tenant_pool_report(db, "tA", FROM, TO)
    assert rep["walks"]["total"] == 0
    assert rep["walks"]["completion_rate"] == 0.0
    assert rep["walkers"]["active"] == 0
    assert rep["revenue"]["walker_amount_total"] == 0.0
    assert rep["top_walkers"] == []


def test_endpoint_report_autz_e_default_period():
    from app.routes.walker_network import tenant_pool_report
    db = _db()
    db.add(Tenant(id="tA", name="A", slug="a")); db.commit()
    admin = User(id="adm", email="a@i", password_hash="x", role="super_admin",
                 is_active=True, token_version=0, must_change_password=False)
    db.add(admin); db.commit()
    out = tenant_pool_report("tA", None, None, admin, db)
    assert out["tenant_id"] == "tA"
    assert out["walks"]["total"] == 0
    assert "from" in out["period"] and "to" in out["period"]
