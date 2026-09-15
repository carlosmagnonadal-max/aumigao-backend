"""S3 — POST/PUT /pets aceitam is_reactive + reactivity_notes; LGPD cobre os campos."""
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.core.database import Base, get_db
from app.dependencies.auth import get_current_user
from app.models.pet import Pet
from app.models.tenant import Tenant
from app.models.user import User
from app.routes import pets
from app.services.tenant_seed_service import DEFAULT_TENANT_SLUG

TENANT_ID = "t-s3-pets"
TUTOR_ID = "tutor-s3-pets"


def build():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(Tenant(id=TENANT_ID, name="Aumigao", slug=DEFAULT_TENANT_SLUG, status="active", plan="business"))
    db.add(User(id=TUTOR_ID, email="tutor-s3@test.com", password_hash="x", role="cliente", tenant_id=TENANT_ID))
    db.commit()
    test_app = FastAPI()
    test_app.include_router(pets.router)
    test_app.dependency_overrides[get_db] = lambda: db
    test_app.dependency_overrides[get_current_user] = lambda: db.get(User, TUTOR_ID)
    return TestClient(test_app), db


def test_create_pet_defaults_not_reactive():
    client, _ = build()
    r = client.post("/pets", json={"name": "Mel"})
    assert r.status_code == 200, r.text
    assert r.json()["is_reactive"] is False
    assert r.json()["reactivity_notes"] is None


def test_create_and_update_reactive_flag_and_notes():
    client, db = build()
    r = client.post("/pets", json={"name": "Thor", "is_reactive": True, "reactivity_notes": "Avança em motos"})
    assert r.status_code == 200, r.text
    pet_id = r.json()["id"]
    assert r.json()["is_reactive"] is True
    assert r.json()["reactivity_notes"] == "Avança em motos"

    r = client.put(f"/pets/{pet_id}", json={"is_reactive": False, "reactivity_notes": ""})
    assert r.status_code == 200, r.text
    db.expire_all()
    pet = db.get(Pet, pet_id)
    assert pet.is_reactive is False and pet.reactivity_notes == ""


def test_update_without_reactive_fields_keeps_stored_value():
    # App antigo (antes da OTA) não envia is_reactive: exclude_unset preserva o valor gravado.
    client, db = build()
    pet_id = client.post("/pets", json={"name": "Thor", "is_reactive": True}).json()["id"]
    r = client.put(f"/pets/{pet_id}", json={"name": "Thor II"})
    assert r.status_code == 200, r.text
    db.expire_all()
    assert db.get(Pet, pet_id).is_reactive is True


def test_reactivity_notes_max_length():
    client, _ = build()
    r = client.post("/pets", json={"name": "Thor", "reactivity_notes": "x" * 1001})
    assert r.status_code == 422


def test_lgpd_export_lists_reactivity_fields():
    from app.services.data_subject_rights_service import _PET_FIELDS

    assert {"is_reactive", "reactivity_notes"} <= set(_PET_FIELDS)
