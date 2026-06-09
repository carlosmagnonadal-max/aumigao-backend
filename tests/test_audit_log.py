import json

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.audit_log import AuditLog
from app.services.audit_service import record_audit_log


def _db():
    engine = create_engine("sqlite:///:memory:")
    AuditLog.__table__.create(engine)
    return sessionmaker(bind=engine)()


def test_record_audit_log_basic():
    db = _db()
    record_audit_log(
        db, action="walker.approved", entity_type="walker", entity_id="w1",
        after={"status": "approved"},
    )
    db.commit()
    row = db.query(AuditLog).first()
    assert row.action == "walker.approved"
    assert row.entity_type == "walker"
    assert row.entity_id == "w1"
    assert json.loads(row.after_data)["status"] == "approved"


def test_record_audit_log_sanitizes_secrets():
    db = _db()
    record_audit_log(
        db, action="user.updated", entity_type="user",
        after={"password": "hunter2", "token": "abc", "email": "a@a.com"},
    )
    db.commit()
    data = json.loads(db.query(AuditLog).first().after_data)
    assert data["password"] == "***"
    assert data["token"] == "***"
    assert data["email"] == "a@a.com"  # dados não-sensíveis preservados


def test_record_audit_log_does_not_commit():
    db = _db()
    record_audit_log(db, action="x", entity_type="y")
    db.rollback()  # sem commit do caller, o registro é descartado (atomicidade)
    assert db.query(AuditLog).count() == 0
