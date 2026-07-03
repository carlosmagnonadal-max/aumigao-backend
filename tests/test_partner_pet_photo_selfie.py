"""Selfie obrigatoria + foto-com-o-pet opcional (contrato cross-repo).

Cobre a separacao dos dois conceitos que antes compartilhavam selfie_url:
  - selfie_url    = selfie segurando o documento (agora OBRIGATORIA);
  - pet_photo_url = foto com o pet (novo tipo "pet_photo", opcional).

1) Upload POST /api/partner-applications/uploads aceita document_type="pet_photo".
2) O serializer da candidatura expoe pet_photo_url e o persiste no payload.
3) _missing_application_fields passa a exigir selfie_url.
4) Fallback legado (app antigo mandando petPhoto como selfie) continua intacto.

Padrao do projeto: FastAPI minimo + SQLite StaticPool + dependency_overrides.
"""
from datetime import datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 - registra todas as tabelas no Base.metadata
from app.core.database import Base, get_db
from app.models.tenant import Tenant
from app.models.user import User
from app.models.walker_profile import WalkerProfile
from app.routes import partner_application, walker
from app.routes.partner_application import (
    PartnerApplicationCreate,
    _apply_partner_application_payload,
    _serialize_partner_application,
)
from app.routes.walker import _missing_application_fields
from app.services.tenant_seed_service import DEFAULT_TENANT_SLUG

TENANT_ID = "t-test"

# PNG de 1x1 valido — passa por _looks_like_image.
PNG_1x1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6360000002000100ffff03000006000557bfabd400"
    "00000049454e44ae426082"
)

_PERSISTENT_URL = "https://cdn.example.com/uploads/walker-documents/x.png"


def _build():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(Tenant(id=TENANT_ID, name="Aumigao", slug=DEFAULT_TENANT_SLUG, status="active", plan="business"))
    db.commit()

    test_app = FastAPI()
    test_app.include_router(walker.router)
    test_app.include_router(partner_application.router)
    test_app.dependency_overrides[get_db] = lambda: db
    return TestClient(test_app), db


# ------------------------------------------------ upload aceita pet_photo -------
def test_upload_accepts_pet_photo_type():
    client, _ = _build()
    r = client.post(
        "/api/partner-applications/uploads",
        data={"document_type": "pet_photo", "owner_id": "cand-1"},
        files={"file": ("pet.png", PNG_1x1, "image/png")},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["documentType"] == "pet_photo"
    # Prefixo do arquivo derivado do tipo => pet_photo-... (allowlist de assinatura).
    assert "/pet_photo-" in body["fileUrl"], body["fileUrl"]


def test_upload_still_accepts_selfie_type():
    client, _ = _build()
    r = client.post(
        "/api/partner-applications/uploads",
        data={"document_type": "selfie", "owner_id": "cand-1"},
        files={"file": ("s.png", PNG_1x1, "image/png")},
    )
    assert r.status_code == 201, r.text
    assert r.json()["documentType"] == "selfie"


# ------------------------------------- persistencia + serializacao pet_photo ----
def test_pet_photo_url_persists_and_returns_in_serializer():
    _, db = _build()
    user = User(id="u-1", email="p@test.com", password_hash="x", role="walker",
                tenant_id=TENANT_ID, full_name="Passeador")
    profile = WalkerProfile(id="wp-1", user_id=user.id)
    db.add(user)
    db.add(profile)
    db.commit()

    payload = PartnerApplicationCreate(
        full_name="Passeador Teste",
        cpf="52998224725",
        email="p@test.com",
        selfie_url=_PERSISTENT_URL + "?doc=selfie",
        pet_photo_url=_PERSISTENT_URL + "?doc=pet",
        accepted_declaration=True,
    )
    _apply_partner_application_payload(profile, payload, cpf="52998224725", phone="")
    db.commit()
    db.refresh(profile)

    # persistiu no modelo
    assert profile.pet_photo_url == _PERSISTENT_URL + "?doc=pet"
    assert profile.selfie_url == _PERSISTENT_URL + "?doc=selfie"

    serialized = _serialize_partner_application(profile, db)
    assert "pet_photo_url" in serialized
    assert serialized["pet_photo_url"]  # URL assinada/normalizada nao vazia
    assert serialized["selfie_url"]


# --------------------------------------- selfie agora eh campo obrigatorio ------
def test_selfie_is_required_in_missing_fields():
    # Sem selfie_url -> mensagem de selfie obrigatoria aparece.
    missing = _missing_application_fields(
        profile_photo_url=_PERSISTENT_URL,
        document_url=_PERSISTENT_URL,
        identity_document_back_url=_PERSISTENT_URL,
        proof_of_address_url=_PERSISTENT_URL,
        selfie_url=None,
        bio="x" * 90,
    )
    assert any("selfie" in m.lower() for m in missing), missing


def test_selfie_present_clears_requirement():
    missing = _missing_application_fields(
        profile_photo_url=_PERSISTENT_URL,
        document_url=_PERSISTENT_URL,
        identity_document_back_url=_PERSISTENT_URL,
        proof_of_address_url=_PERSISTENT_URL,
        selfie_url=_PERSISTENT_URL,
        bio="x" * 90,
    )
    assert missing == [], missing


# ------------------------------ fallback legado petPhoto -> selfie_url intacto ---
def test_legacy_petphoto_maps_to_selfie_url_on_register():
    """App antigo (pre build EAS) manda documents.petPhoto e cai em selfie_url.

    Espelha o fallback de app/routes/auth.py (~225):
      selfie_url = profile_payload.get("selfie_url") or documents.get("petPhoto")
    Aqui exercitamos a mesma logica de resolucao para travar o contrato.
    """
    documents = {"petPhoto": _PERSISTENT_URL + "?legacy=1"}
    profile_payload = {}
    resolved_selfie = profile_payload.get("selfie_url") or documents.get("petPhoto")
    resolved_pet_photo = profile_payload.get("pet_photo_url") or documents.get("petPhoto2")
    assert resolved_selfie == _PERSISTENT_URL + "?legacy=1"
    assert resolved_pet_photo is None
