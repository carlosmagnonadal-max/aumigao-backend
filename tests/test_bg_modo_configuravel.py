"""BG-modo — verificacao configuravel por tenant: 'selo' vs 'gate'.

Cobre:
- background_check_mode helper (unit tests puros, sem banco de producao)
- Modo "selo" (NOVO DEFAULT): aprovacao nunca bloqueada, mesmo sem verificacao
- Modo "gate" (comportamento original): bloqueia; override+justificativa continua
- Default sem limit_value => "selo"
- PATCH de limit_value persiste (via rota de features existente)
- Flag OFF ignora tudo (zero regressao)
- Serializer expoe background_check_mode no payload
"""
from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
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
from app.routes import tenants as tenants_router
from app.services.background_check_service import (
    background_check_mode,
    BG_MODE_GATE,
    BG_MODE_SELO,
)
from app.services.tenant_seed_service import DEFAULT_TENANT_SLUG

ADMIN_ID = "adm-modo-1"
CAND_ID = "cand-modo-1"
CAND_USER_ID = "cand-user-modo-1"
TENANT_ID = "t-modo-bg"


# ---------------------------------------------------------------------------
# Helper de construcao de cenario
# ---------------------------------------------------------------------------

def build(
    *,
    profile_status: str = "under_review",
    flag_on: bool = False,
    bg_limit_value: str | None = None,
    bg_status: str = "none",
):
    """Monta app de teste com SQLite em memoria.

    flag_on: True => insere TenantFeature(enabled=True) com background_checks.
    bg_limit_value: "gate" | "selo" | None => define limit_value da TenantFeature.
    """
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(Tenant(
        id=TENANT_ID, name="Aumigao", slug=DEFAULT_TENANT_SLUG,
        status="active", plan="business",
    ))
    db.add(User(
        id=ADMIN_ID, email="adm-modo@test.com", password_hash="x",
        role="super_admin", full_name="Admin Modo", tenant_id=TENANT_ID,
    ))
    db.add(User(
        id=CAND_USER_ID, email="cand-modo@test.com", password_hash="x",
        role="cliente", full_name="Candidato Modo", tenant_id=TENANT_ID,
    ))
    db.add(WalkerProfile(
        id=CAND_ID, user_id=CAND_USER_ID, full_name="Candidato Modo",
        cpf="52998224725", phone="11987654321", city="Sao Paulo", state="SP",
        status=profile_status, background_check_status=bg_status,
        created_at=datetime.utcnow(),
    ))
    if flag_on:
        db.add(TenantFeature(
            id=str(uuid4()), tenant_id=TENANT_ID,
            feature_key="background_checks", enabled=True,
            limit_value=bg_limit_value,
        ))
    db.commit()

    test_app = FastAPI()
    test_app.include_router(admin.router)
    # tenants_router.router ja tem prefix="/admin/tenants" embutido
    test_app.include_router(tenants_router.router)
    test_app.dependency_overrides[get_db] = lambda: db
    test_app.dependency_overrides[get_current_user] = lambda: db.get(User, ADMIN_ID)
    return TestClient(test_app), db


def _add_cert(db, cert_type: str, status: str, uf: str | None = None):
    cert = WalkerBackgroundCertificate(
        id=str(uuid4()), walker_profile_id=CAND_ID, cert_type=cert_type,
        issuer_uf=uf, cert_number=f"{cert_type}-modo", status=status,
    )
    db.add(cert)
    db.commit()
    return cert


# ---------------------------------------------------------------------------
# Unit tests: background_check_mode helper
# ---------------------------------------------------------------------------

class _FakeRow:
    def __init__(self, limit_value):
        self.limit_value = limit_value


class _FakeQuery:
    def __init__(self, row):
        self._row = row

    def filter(self, *_):
        return self

    def first(self):
        return self._row


class _FakeDB:
    """Stub minimo de Session para testar background_check_mode sem banco real."""
    def __init__(self, row):
        self._row = row

    def query(self, model):
        return _FakeQuery(self._row)


def test_mode_helper_none_tenant_retorna_selo():
    # tenant_id None => sempre "selo"
    db = _FakeDB(None)
    assert background_check_mode(db, None) == BG_MODE_SELO


def test_mode_helper_sem_linha_retorna_selo():
    # Nenhuma TenantFeature encontrada => "selo"
    db = _FakeDB(None)
    assert background_check_mode(db, "qualquer-tenant") == BG_MODE_SELO


def test_mode_helper_limit_value_gate():
    row = _FakeRow("gate")
    db = _FakeDB(row)
    assert background_check_mode(db, "t-1") == BG_MODE_GATE


def test_mode_helper_limit_value_selo_explicito():
    row = _FakeRow("selo")
    db = _FakeDB(row)
    assert background_check_mode(db, "t-1") == BG_MODE_SELO


def test_mode_helper_limit_value_none():
    row = _FakeRow(None)
    db = _FakeDB(row)
    assert background_check_mode(db, "t-1") == BG_MODE_SELO


def test_mode_helper_limit_value_vazio():
    row = _FakeRow("   ")
    db = _FakeDB(row)
    assert background_check_mode(db, "t-1") == BG_MODE_SELO


def test_mode_helper_limit_value_desconhecido():
    # Valor desconhecido cai em "selo" (fail-open)
    row = _FakeRow("outro-valor")
    db = _FakeDB(row)
    assert background_check_mode(db, "t-1") == BG_MODE_SELO


def test_mode_helper_gate_case_insensitive():
    # "GATE" em maiusculo deve ser normalizado para gate
    row = _FakeRow("GATE")
    db = _FakeDB(row)
    assert background_check_mode(db, "t-1") == BG_MODE_GATE


# ---------------------------------------------------------------------------
# Testes de integracao: modo "selo" (NOVO DEFAULT)
# ---------------------------------------------------------------------------

def test_modo_selo_sem_verificacao_aprova_normalmente():
    """Modo selo + sem antecedentes => aprovacao OK sem bloquear."""
    client, db = build(flag_on=True, bg_limit_value=None, bg_status="none")
    r = client.post(f"/admin/walkers/{CAND_ID}/approve")
    assert r.status_code == 200, r.text
    assert r.json()["raw_status"] == "active"
    assert db.get(WalkerProfile, CAND_ID).status == "active"


def test_modo_selo_flagged_aprova_normalmente():
    """Modo selo + antecedentes flagged => aprovacao OK (nao bloqueia)."""
    client, db = build(flag_on=True, bg_limit_value="selo", bg_status="flagged")
    _add_cert(db, "pf", "rejected")
    r = client.post(f"/admin/walkers/{CAND_ID}/approve")
    assert r.status_code == 200, r.text
    assert r.json()["raw_status"] == "active"


def test_modo_selo_sem_override_nao_exige_justificativa():
    """Modo selo => aprovacao sem nenhum payload de override funciona."""
    client, db = build(flag_on=True, bg_limit_value="selo", bg_status="submitted")
    r = client.post(f"/admin/walkers/{CAND_ID}/approve", json={})
    assert r.status_code == 200, r.text


def test_modo_selo_explicito_aprova_sem_verificacao():
    """limit_value='selo' explicitado => comportamento igual a NULL."""
    client, db = build(flag_on=True, bg_limit_value="selo", bg_status="none")
    r = client.post(f"/admin/walkers/{CAND_ID}/approve")
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# Testes de integracao: modo "gate" (comportamento original preservado)
# ---------------------------------------------------------------------------

def test_modo_gate_sem_verificacao_bloqueia():
    """Modo gate + nao verificado => 409 (comportamento original)."""
    client, db = build(flag_on=True, bg_limit_value="gate", bg_status="none")
    r = client.post(f"/admin/walkers/{CAND_ID}/approve")
    assert r.status_code == 409, r.text
    assert db.get(WalkerProfile, CAND_ID).status == "under_review"


def test_modo_gate_override_com_justificativa_aprova():
    """Modo gate + override+justificativa => aprova normalmente."""
    client, db = build(flag_on=True, bg_limit_value="gate", bg_status="none")
    r = client.post(
        f"/admin/walkers/{CAND_ID}/approve",
        json={"override": True, "override_justification": "Conferido manualmente."},
    )
    assert r.status_code == 200, r.text
    assert db.get(WalkerProfile, CAND_ID).status == "active"


def test_modo_gate_override_sem_justificativa_bloqueia():
    """Modo gate + override sem justificativa => ainda bloqueia (409)."""
    client, db = build(flag_on=True, bg_limit_value="gate", bg_status="none")
    r = client.post(
        f"/admin/walkers/{CAND_ID}/approve",
        json={"override": True, "override_justification": "  "},
    )
    assert r.status_code == 409, r.text


def test_modo_gate_verificado_aprova_sem_override():
    """Modo gate + antecedentes verified => aprova sem precisar de override."""
    client, db = build(flag_on=True, bg_limit_value="gate")
    _add_cert(db, "pf", "validated")
    _add_cert(db, "tj", "validated", uf="SP")
    r = client.post(f"/admin/walkers/{CAND_ID}/approve")
    assert r.status_code == 200, r.text
    assert db.get(WalkerProfile, CAND_ID).status == "active"


def test_modo_gate_partial_bloqueia():
    """Modo gate + antecedentes partial (so PF validada, TJ pendente) => 409."""
    client, db = build(flag_on=True, bg_limit_value="gate")
    _add_cert(db, "pf", "validated")
    _add_cert(db, "tj", "pending", uf="SP")
    r = client.post(f"/admin/walkers/{CAND_ID}/approve")
    assert r.status_code == 409, r.text


# ---------------------------------------------------------------------------
# Default sem limit_value = "selo"
# ---------------------------------------------------------------------------

def test_default_sem_limit_value_e_selo():
    """TenantFeature com limit_value=None => modo "selo" (novo default)."""
    client, db = build(flag_on=True, bg_limit_value=None, bg_status="none")
    r = client.post(f"/admin/walkers/{CAND_ID}/approve")
    # Modo selo => aprovacao sem bloquear
    assert r.status_code == 200, r.text
    assert r.json()["raw_status"] == "active"


# ---------------------------------------------------------------------------
# Flag OFF => zero regressao (ignora modo completamente)
# ---------------------------------------------------------------------------

def test_flag_off_ignora_modo_e_aprova():
    """Flag background_checks OFF => modo ignorado; aprovacao livre."""
    client, db = build(flag_on=False, bg_status="none")
    r = client.post(f"/admin/walkers/{CAND_ID}/approve")
    assert r.status_code == 200, r.text
    assert r.json()["raw_status"] == "active"


# ---------------------------------------------------------------------------
# Serializer expoe background_check_mode
# ---------------------------------------------------------------------------

def test_serializer_expoe_background_check_mode_selo():
    """GET do perfil expoe background_check_mode='selo' quando flag ON sem gate."""
    client, db = build(flag_on=True, bg_limit_value=None)
    r = client.get(f"/admin/partner-applications/{CAND_ID}")
    assert r.status_code == 200, r.text
    assert r.json()["background_check_mode"] == BG_MODE_SELO


def test_serializer_expoe_background_check_mode_gate():
    """GET do perfil expoe background_check_mode='gate' quando limit_value='gate'."""
    client, db = build(flag_on=True, bg_limit_value="gate")
    r = client.get(f"/admin/partner-applications/{CAND_ID}")
    assert r.status_code == 200, r.text
    assert r.json()["background_check_mode"] == BG_MODE_GATE


def test_serializer_expoe_background_check_mode_flag_off():
    """GET do perfil expoe background_check_mode='selo' mesmo com flag OFF (default)."""
    client, db = build(flag_on=False)
    r = client.get(f"/admin/partner-applications/{CAND_ID}")
    assert r.status_code == 200, r.text
    # Flag OFF => nenhuma TenantFeature => modo = "selo" (default)
    assert r.json()["background_check_mode"] == BG_MODE_SELO


# ---------------------------------------------------------------------------
# PATCH limit_value persiste (rota de features existente)
# ---------------------------------------------------------------------------

def test_patch_limit_value_persiste():
    """PATCH /tenants/{id}/features persiste limit_value='gate' na TenantFeature."""
    client, db = build(flag_on=True, bg_limit_value=None)
    r = client.patch(
        f"/admin/tenants/{TENANT_ID}/features",
        json=[{"feature_key": "background_checks", "enabled": True, "limit_value": "gate"}],
    )
    assert r.status_code == 200, r.text
    row = (
        db.query(TenantFeature)
        .filter(TenantFeature.tenant_id == TENANT_ID, TenantFeature.feature_key == "background_checks")
        .first()
    )
    assert row is not None
    assert row.limit_value == "gate"


def test_patch_limit_value_null_persiste():
    """PATCH com limit_value=None limpa o valor (reset para selo)."""
    client, db = build(flag_on=True, bg_limit_value="gate")
    r = client.patch(
        f"/admin/tenants/{TENANT_ID}/features",
        json=[{"feature_key": "background_checks", "enabled": True, "limit_value": None}],
    )
    assert r.status_code == 200, r.text
    row = (
        db.query(TenantFeature)
        .filter(TenantFeature.tenant_id == TENANT_ID, TenantFeature.feature_key == "background_checks")
        .first()
    )
    assert row is not None
    assert row.limit_value is None


def test_patch_limit_value_selo_persiste_e_aprova():
    """Apos PATCH com limit_value='selo', aprovacao nao e mais bloqueada.

    Valida a sequencia: banco inicia com modo gate; PATCH muda para selo;
    aprovacao passa sem bloquear. Evita chamar approve com gate (que geraria
    409 e poderia sujar o estado da sessao SQLite em memoria).
    """
    # Cenario: flag ON com modo gate e sem verificacao
    client, db = build(flag_on=True, bg_limit_value="gate", bg_status="none")

    # Confirma estado inicial: limit_value == "gate"
    feat_row = (
        db.query(TenantFeature)
        .filter(TenantFeature.tenant_id == TENANT_ID, TenantFeature.feature_key == "background_checks")
        .first()
    )
    assert feat_row is not None
    assert feat_row.limit_value == "gate"

    # Muda para selo via PATCH
    r_patch = client.patch(
        f"/admin/tenants/{TENANT_ID}/features",
        json=[{"feature_key": "background_checks", "enabled": True, "limit_value": "selo"}],
    )
    assert r_patch.status_code == 200, r_patch.text

    # Confirma persistencia do novo valor
    db.expire_all()
    feat_row_after = (
        db.query(TenantFeature)
        .filter(TenantFeature.tenant_id == TENANT_ID, TenantFeature.feature_key == "background_checks")
        .first()
    )
    assert feat_row_after is not None
    assert feat_row_after.limit_value == "selo"

    # Agora aprova sem bloquear (modo selo)
    r_approve = client.post(f"/admin/walkers/{CAND_ID}/approve")
    assert r_approve.status_code == 200, r_approve.text
    assert r_approve.json()["raw_status"] == "active"
