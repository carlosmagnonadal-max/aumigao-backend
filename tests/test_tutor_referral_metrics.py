from __future__ import annotations

import app.models  # noqa: F401

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.models.tenant import Tenant
from app.models.tutor_referral import TutorReferral
from app.dependencies.tenant_scope import AdminTenantScope
from app.services.metrics_service import get_tutor_referral_metrics


def _db():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    db.add(Tenant(id="t1", name="T1", slug="t1", status="active", plan="business"))
    db.add(Tenant(id="t2", name="T2", slug="t2", status="active", plan="business"))
    db.add(
        TutorReferral(
            id="a",
            tenant_id="t1",
            referrer_user_id="u1",
            referral_code="C1",
            status="pending",
            reward_status="not_eligible",
        )
    )
    db.add(
        TutorReferral(
            id="b",
            tenant_id="t1",
            referrer_user_id="u1",
            referred_user_id="u2",
            referral_code="C2",
            status="registered",
            reward_status="not_eligible",
        )
    )
    db.add(
        TutorReferral(
            id="c",
            tenant_id="t1",
            referrer_user_id="u1",
            referred_user_id="u3",
            referral_code="C3",
            status="converted",
            reward_status="granted",
        )
    )
    db.add(
        TutorReferral(
            id="d",
            tenant_id="t2",
            referrer_user_id="u9",
            referred_user_id="u8",
            referral_code="C4",
            status="converted",
            reward_status="granted",
        )
    )
    db.commit()
    return db


def _scope(tenant_id: str) -> AdminTenantScope:
    # AdminTenantScope is a frozen dataclass: user, tenant_id, is_global, role
    # apply_tenant_filter only reads is_global and tenant_id, so user=None is safe here.
    return AdminTenantScope(user=None, tenant_id=tenant_id, is_global=False, role="admin")


def test_metrics_tenant_scoped():
    db = _db()
    data = get_tutor_referral_metrics(db, _scope("t1"))
    assert data["total"] == 3
    assert data["converted_count"] == 1
    assert data["granted_count"] == 1
    assert {s["status"]: s["count"] for s in data["by_status"]} == {
        "pending": 1,
        "registered": 1,
        "converted": 1,
    }
