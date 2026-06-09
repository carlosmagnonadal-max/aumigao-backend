from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.upload_file import UploadFile
from app.services.upload_registry import record_upload


def _db():
    engine = create_engine("sqlite:///:memory:")
    UploadFile.__table__.create(engine)
    return sessionmaker(bind=engine)()


def test_record_upload_persists_metadata():
    db = _db()
    record_upload(
        db, context="partner_application", owner_id="o1", document_type="selfie",
        storage_path="/uploads/walker-documents/o1/selfie-x.jpg",
        mime_type="image/jpeg", size_bytes=12345,
    )
    db.commit()
    row = db.query(UploadFile).first()
    assert row.context == "partner_application"
    assert row.owner_id == "o1"
    assert row.document_type == "selfie"
    assert row.size_bytes == 12345


def test_record_upload_does_not_commit():
    db = _db()
    record_upload(db, context="pet", storage_path="/x.jpg")
    db.rollback()  # sem commit do caller, o registro é descartado
    assert db.query(UploadFile).count() == 0
