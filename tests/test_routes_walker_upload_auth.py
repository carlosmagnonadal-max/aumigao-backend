"""sec-SEC4 — regressao de AUTH nos uploads de documento/foto do passeador.

Cobre os dois endpoints de UploadFile de app/routes/walker.py:

1) POST /walker/walks/{walk_id}/completion-photo (foto de finalizacao):
   JA exige auth e deriva o dono do TOKEN. Travamos:
     - 401 sem token (HTTPBearer auto_error=False);
     - 403 quando o passeio NAO pertence ao passeador do token
       (um walker nao envia foto no passeio de outro).

2) POST /api/partner-applications/uploads (documentos de credenciamento —
   RG/CPF/selfie/comprovante): e PRE-conta por design (a candidatura cria o
   usuario depois, em create_partner_application), entao NAO exige token.
   Documentamos esse contrato atual (201 sem auth) como regressao + anotamos a
   pendencia de derivar o owner do token quando o fluxo for pos-login.

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
from app.dependencies.auth import get_current_user
from app.models.tenant import Tenant
from app.models.user import User
from app.models.walk import Walk
from app.models.walker_profile import WalkerProfile
from app.routes import partner_application, walker
from app.services.tenant_seed_service import DEFAULT_TENANT_SLUG

TENANT_ID = "t-test"
WALKER_ID = "walker-owner"
OTHER_WALKER_ID = "walker-intruder"
WALK_ID = "walk-1"

# PNG de 1x1 valido (passa por _looks_like_image) — so usado nos casos em que a
# requisicao chega ate a leitura do arquivo. Nos casos de 401/403 nem e lido.
PNG_1x1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6360000002000100ffff03000006000557bfabd400"
    "00000049454e44ae426082"
)


def _seed(db):
    db.add(Tenant(id=TENANT_ID, name="Aumigao", slug=DEFAULT_TENANT_SLUG, status="active", plan="business"))
    for uid in (WALKER_ID, OTHER_WALKER_ID):
        db.add(User(id=uid, email=f"{uid}@test.com", password_hash="x", role="walker",
                    tenant_id=TENANT_ID, full_name=uid))
        db.add(WalkerProfile(id=f"wp-{uid}", user_id=uid, full_name=uid,
                             status="active", active_as_walker=True))
    # passeio que pertence ao WALKER_ID, em status que permite foto de finalizacao
    db.add(Walk(id=WALK_ID, tutor_id=WALKER_ID, tenant_id=TENANT_ID, walker_id=WALKER_ID,
                pet_id="pet-1", scheduled_date="2026-06-16", duration_minutes=30,
                price=50.0, operational_status="ride_in_progress"))
    db.commit()


def _build(current_user_id: str | None):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    _seed(db)

    test_app = FastAPI()
    test_app.include_router(walker.router)
    test_app.include_router(partner_application.router)
    test_app.dependency_overrides[get_db] = lambda: db
    if current_user_id is not None:
        test_app.dependency_overrides[get_current_user] = lambda: db.get(User, current_user_id)
    return TestClient(test_app), db


# ----------------------------------------- completion-photo: auth obrigatoria --
def test_completion_photo_401_without_token():
    # Sem override de get_current_user -> HTTPBearer auto_error=False -> 401.
    client, _ = _build(current_user_id=None)
    r = client.post(
        f"/walker/walks/{WALK_ID}/completion-photo",
        files={"file": ("p.png", PNG_1x1, "image/png")},
    )
    assert r.status_code == 401, r.text


def test_completion_photo_403_when_walk_belongs_to_other_walker():
    # Passeador autenticado != dono do passeio -> 403 (owner derivado do token).
    client, _ = _build(current_user_id=OTHER_WALKER_ID)
    r = client.post(
        f"/walker/walks/{WALK_ID}/completion-photo",
        files={"file": ("p.png", PNG_1x1, "image/png")},
    )
    assert r.status_code == 403, r.text
    assert "passeador" in r.json()["detail"].lower()


# ---------------------------- credenciamento docs: pre-conta (contrato atual) --
def test_partner_application_upload_is_preauth_by_design():
    # Documentos de credenciamento sao enviados ANTES da conta existir, logo o
    # endpoint nao exige token. Travamos o contrato atual (nao quebrar o
    # onboarding) — ver pendencia SEC4 sobre derivar owner do token pos-login.
    client, _ = _build(current_user_id=None)
    r = client.post(
        "/api/partner-applications/uploads",
        data={"document_type": "selfie", "owner_id": "cand-1"},
        files={"file": ("s.png", PNG_1x1, "image/png")},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["documentType"] == "selfie"
    assert body["reviewStatus"] == "pending_review"
