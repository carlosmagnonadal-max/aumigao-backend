from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.incentive_rule import IncentiveRule
from app.models.tenant import Tenant
from app.models.user import User
from app.models.walker_incentive import WalkerIncentive
from app.services import incentive_rule_service as svc

TENANT_A = "tA"
TENANT_B = "tB"


def _db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[
            IncentiveRule.__table__,
            Tenant.__table__,
            User.__table__,
            WalkerIncentive.__table__,
        ],
    )
    return sessionmaker(bind=engine)()


def _tenant(db, tid, slug):
    db.add(Tenant(id=tid, name=tid, slug=slug, status="active", plan="starter"))
    db.commit()


def _walker(db, *, user_id, tenant_id):
    db.add(User(id=user_id, email=f"{user_id}@x.com", password_hash="x", tenant_id=tenant_id, role="walker"))
    db.commit()


def _base(db):
    _tenant(db, TENANT_A, "a")
    _tenant(db, TENANT_B, "b")


# --------------------------------------------------------------------------- #
# CRUD
# --------------------------------------------------------------------------- #
def test_create_rule_ok():
    db = _db()
    _base(db)
    rule = svc.create_rule(TENANT_A, {
        "key": "k1", "title": "T", "description": "", "trigger_type": "rating",
        "threshold": 4.8, "reward_type": "recognition", "reward_value": 0.0,
        "visibility_effect": "none", "active": True,
    }, db)
    assert rule.id and rule.tenant_id == TENANT_A and rule.key == "k1"


def test_create_rule_duplicate_key_conflicts():
    db = _db()
    _base(db)
    data = {"key": "dup", "title": "T", "description": "", "trigger_type": "rating",
            "threshold": 1.0, "reward_type": "recognition", "reward_value": 0.0,
            "visibility_effect": "none", "active": True}
    svc.create_rule(TENANT_A, dict(data), db)
    with pytest.raises(HTTPException) as exc:
        svc.create_rule(TENANT_A, dict(data), db)
    assert exc.value.status_code == 409


def test_same_key_allowed_across_tenants():
    db = _db()
    _base(db)
    data = {"key": "shared", "title": "T", "description": "", "trigger_type": "rating",
            "threshold": 1.0, "reward_type": "recognition", "reward_value": 0.0,
            "visibility_effect": "none", "active": True}
    a = svc.create_rule(TENANT_A, dict(data), db)
    b = svc.create_rule(TENANT_B, dict(data), db)
    assert a.id != b.id


def test_create_rule_invalid_trigger_type():
    db = _db()
    _base(db)
    with pytest.raises(HTTPException) as exc:
        svc.create_rule(TENANT_A, {
            "key": "k", "title": "T", "description": "", "trigger_type": "bogus",
            "threshold": 1.0, "reward_type": "recognition", "reward_value": 0.0,
            "visibility_effect": "none", "active": True,
        }, db)
    assert exc.value.status_code == 422


def test_create_rule_invalid_reward_type():
    db = _db()
    _base(db)
    with pytest.raises(HTTPException) as exc:
        svc.create_rule(TENANT_A, {
            "key": "k", "title": "T", "description": "", "trigger_type": "rating",
            "threshold": 1.0, "reward_type": "bogus", "reward_value": 0.0,
            "visibility_effect": "none", "active": True,
        }, db)
    assert exc.value.status_code == 422


def test_update_rule_ok():
    db = _db()
    _base(db)
    rule = svc.create_rule(TENANT_A, {
        "key": "k", "title": "Old", "description": "", "trigger_type": "rating",
        "threshold": 1.0, "reward_type": "recognition", "reward_value": 0.0,
        "visibility_effect": "none", "active": True,
    }, db)
    updated = svc.update_rule(TENANT_A, rule.id, {"title": "New", "active": False}, db)
    assert updated.title == "New" and updated.active is False


def test_update_rule_other_tenant_404():
    db = _db()
    _base(db)
    rule = svc.create_rule(TENANT_A, {
        "key": "k", "title": "T", "description": "", "trigger_type": "rating",
        "threshold": 1.0, "reward_type": "recognition", "reward_value": 0.0,
        "visibility_effect": "none", "active": True,
    }, db)
    with pytest.raises(HTTPException) as exc:
        svc.update_rule(TENANT_B, rule.id, {"title": "X"}, db)
    assert exc.value.status_code == 404


def test_list_rules_scoped_by_tenant():
    db = _db()
    _base(db)
    for t in (TENANT_A, TENANT_A, TENANT_B):
        svc.create_rule(t, {
            "key": uuid4().hex[:8], "title": "T", "description": "", "trigger_type": "rating",
            "threshold": 1.0, "reward_type": "recognition", "reward_value": 0.0,
            "visibility_effect": "none", "active": True,
        }, db)
    assert len(svc.list_rules(TENANT_A, db)) == 2
    assert len(svc.list_rules(TENANT_B, db)) == 1


# --------------------------------------------------------------------------- #
# grant / revoke / list concessoes
# --------------------------------------------------------------------------- #
def test_grant_manual_records_amount():
    db = _db()
    _base(db)
    _walker(db, user_id="w1", tenant_id=TENANT_A)
    payload = svc.grant_manual(TENANT_A, "w1", {
        "incentive_type": None, "title": "Bonus", "description": "", "source": "admin",
        "visibility_effect": "none", "reward_type": "monetary", "amount": 30.0,
        "expires_at": None, "admin_notes": None,
    }, db)
    assert payload["reward_type"] == "monetary"
    assert payload["amount"] == 30.0
    assert payload["incentive_type"] == "monetary"


def test_grant_manual_walker_other_tenant_404():
    db = _db()
    _base(db)
    _walker(db, user_id="w1", tenant_id=TENANT_B)
    with pytest.raises(HTTPException) as exc:
        svc.grant_manual(TENANT_A, "w1", {"title": "X", "reward_type": "recognition", "amount": 0}, db)
    assert exc.value.status_code == 404


def test_revoke_granted_ok():
    db = _db()
    _base(db)
    _walker(db, user_id="w1", tenant_id=TENANT_A)
    granted = svc.grant_manual(TENANT_A, "w1", {"title": "X", "reward_type": "recognition", "amount": 0}, db)
    revoked = svc.revoke_granted(TENANT_A, granted["id"], db, admin_notes="abuso")
    assert revoked["status"] == "revoked"


def test_revoke_granted_other_tenant_404():
    db = _db()
    _base(db)
    _walker(db, user_id="w1", tenant_id=TENANT_A)
    granted = svc.grant_manual(TENANT_A, "w1", {"title": "X", "reward_type": "recognition", "amount": 0}, db)
    with pytest.raises(HTTPException) as exc:
        svc.revoke_granted(TENANT_B, granted["id"], db)
    assert exc.value.status_code == 404


def test_list_granted_scoped_by_tenant():
    db = _db()
    _base(db)
    _walker(db, user_id="w1", tenant_id=TENANT_A)
    _walker(db, user_id="w2", tenant_id=TENANT_B)
    svc.grant_manual(TENANT_A, "w1", {"title": "A1", "reward_type": "recognition", "amount": 0}, db)
    svc.grant_manual(TENANT_B, "w2", {"title": "B1", "reward_type": "recognition", "amount": 0}, db)
    items_a = svc.list_granted(TENANT_A, db)
    assert {i["title"] for i in items_a} == {"A1"}


def test_list_granted_filter_by_walker_and_status():
    db = _db()
    _base(db)
    _walker(db, user_id="w1", tenant_id=TENANT_A)
    _walker(db, user_id="w2", tenant_id=TENANT_A)
    svc.grant_manual(TENANT_A, "w1", {"title": "T1", "reward_type": "recognition", "amount": 0}, db)
    svc.grant_manual(TENANT_A, "w2", {"title": "T2", "reward_type": "recognition", "amount": 0}, db)
    items = svc.list_granted(TENANT_A, db, walker_id="w1")
    assert {i["walker_id"] for i in items} == {"w1"}
    active = svc.list_granted(TENANT_A, db, status="active")
    assert len(active) == 2
    revoked = svc.list_granted(TENANT_A, db, status="revoked")
    assert revoked == []
