"""BG-3 — admin: serialize de certidoes + PATCH validar/rejeitar + GATE no approve.

Foco do gate: provar ZERO regressao com a flag OFF (default) e bloqueio/override
com a flag ON. Reusa o padrao de build do test_routes_admin_walker_approval.
"""
from datetime import datetime
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.core.database import Base, get_db
from app.dependencies.auth import get_current_user
from app.models.tenant import Tenant, TenantFeature
from app.models.user import User
from app.models.walker_profile import WalkerProfile
from app.models.walker_background_certificate import WalkerBackgroundCertificate
from app.routes import admin
from app.services.tenant_seed_service import DEFAULT_TENANT_SLUG

ADMIN_ID = "admin-1"
CAND_ID = "cand-1"
CAND_USER_ID = "candidato-user-1"
TENANT_ID = "t-bg"


def build(*, profile_status: str = "under_review", flag_on: bool = False, bg_status: str = "none", bg_limit_value: str | None = None):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(Tenant(id=TENANT_ID, name="Aumigao", slug=DEFAULT_TENANT_SLUG, status="active", plan="business"))
    db.add(User(id=ADMIN_ID, email="adm@correio.com", password_hash="x", role="super_admin", full_name="Administrador", tenant_id=TENANT_ID))
    db.add(User(id=CAND_USER_ID, email="joao.silva@correio.com", password_hash="x", role="cliente", full_name="Joao Silva", tenant_id=TENANT_ID))
    db.add(WalkerProfile(
        id=CAND_ID, user_id=CAND_USER_ID, full_name="Joao Silva",
        cpf="52998224725", phone="11987654321", city="Sao Paulo", state="SP",
        status=profile_status, background_check_status=bg_status, created_at=datetime.utcnow(),
    ))
    if flag_on:
        db.add(TenantFeature(id=str(uuid4()), tenant_id=TENANT_ID, feature_key="background_checks", enabled=True, limit_value=bg_limit_value))
    db.commit()

    test_app = FastAPI()
    test_app.include_router(admin.router)
    test_app.dependency_overrides[get_db] = lambda: db
    test_app.dependency_overrides[get_current_user] = lambda: db.get(User, ADMIN_ID)
    return TestClient(test_app), db


def _add_cert(db, cert_type, status, uf=None):
    cert = WalkerBackgroundCertificate(
        id=str(uuid4()), walker_profile_id=CAND_ID, cert_type=cert_type,
        issuer_uf=uf, cert_number=f"{cert_type}-1", status=status,
    )
    db.add(cert)
    db.commit()
    return cert


# ------------------------------------------------------------------ serialize ---
def test_serialize_includes_background_fields():
    client, db = build()
    _add_cert(db, "pf", "pending")
    r = client.get(f"/admin/partner-applications/{CAND_ID}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["background_check_status"] == "submitted"
    assert len(body["background_certificates"]) == 1
    cert = body["background_certificates"][0]
    assert cert["cert_type"] == "pf"
    assert "servicos.pf.gov.br" in cert["official_validation_url"]


# ------------------------------------------------------- PATCH validar/rejeitar ---
def test_patch_validate_certificate():
    client, db = build()
    cert = _add_cert(db, "pf", "pending")
    r = client.patch(
        f"/admin/partner-applications/{CAND_ID}/background-certificate/{cert.id}",
        json={"status": "validated"},
    )
    assert r.status_code == 200, r.text
    row = db.get(WalkerBackgroundCertificate, cert.id)
    assert row.status == "validated"
    assert row.validated_by_admin_id == ADMIN_ID
    assert row.validated_at is not None


def test_patch_reject_certificate_flags_aggregate():
    client, db = build()
    cert = _add_cert(db, "pf", "pending")
    r = client.patch(
        f"/admin/partner-applications/{CAND_ID}/background-certificate/{cert.id}",
        json={"status": "rejected", "notes": "homonimo"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["background_check_status"] == "flagged"


def test_patch_invalid_status_400():
    client, db = build()
    cert = _add_cert(db, "pf", "pending")
    r = client.patch(
        f"/admin/partner-applications/{CAND_ID}/background-certificate/{cert.id}",
        json={"status": "banana"},
    )
    assert r.status_code == 400


def test_patch_cert_404_unknown():
    client, _ = build()
    r = client.patch(
        f"/admin/partner-applications/{CAND_ID}/background-certificate/nope",
        json={"status": "validated"},
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------- GATE ---
def test_gate_flag_off_approve_works_zero_regression():
    # Flag OFF (default) + sem antecedentes -> aprova normalmente (comportamento atual).
    client, db = build(flag_on=False, bg_status="none")
    r = client.post(f"/admin/walkers/{CAND_ID}/approve")
    assert r.status_code == 200, r.text
    assert r.json()["raw_status"] == "active"
    assert db.get(WalkerProfile, CAND_ID).status == "active"


def test_gate_flag_on_unverified_blocks():
    # Modo "gate" explicito: aprovacao bloqueada sem verificacao.
    client, db = build(flag_on=True, bg_status="none", bg_limit_value="gate")
    r = client.post(f"/admin/walkers/{CAND_ID}/approve")
    assert r.status_code == 409, r.text
    # nao aprovou
    assert db.get(WalkerProfile, CAND_ID).status == "under_review"


def test_gate_flag_on_override_with_justification_approves():
    # Modo "gate" explicito: override+justificativa permite aprovar.
    client, db = build(flag_on=True, bg_status="none", bg_limit_value="gate")
    r = client.post(
        f"/admin/walkers/{CAND_ID}/approve",
        json={"override": True, "override_justification": "Documentacao conferida fora do sistema."},
    )
    assert r.status_code == 200, r.text
    assert db.get(WalkerProfile, CAND_ID).status == "active"


def test_gate_flag_on_override_without_justification_blocks():
    # Modo "gate" explicito: override sem justificativa ainda bloqueia.
    client, db = build(flag_on=True, bg_status="none", bg_limit_value="gate")
    r = client.post(f"/admin/walkers/{CAND_ID}/approve", json={"override": True, "override_justification": "  "})
    assert r.status_code == 409, r.text


def test_gate_flag_on_verified_approves():
    # PF + TJ validadas -> verified -> aprova sem override (modo gate tambem).
    client, db = build(flag_on=True, bg_limit_value="gate")
    _add_cert(db, "pf", "validated")
    _add_cert(db, "tj", "validated", uf="SP")
    r = client.post(f"/admin/walkers/{CAND_ID}/approve")
    assert r.status_code == 200, r.text
    assert db.get(WalkerProfile, CAND_ID).status == "active"
