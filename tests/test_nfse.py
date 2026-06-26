"""Testes TDD — NFS-e via Asaas (dormente por NFS_E_ENABLED=false).

Cobre:
- Flag OFF → issue_nfse_for_saas_payment retorna None, zero linhas, zero HTTP.
- Flag ON + mock sucesso → cria Nfse status=scheduled; 2ª chamada idempotente.
- Flag ON + mock que levanta → cria Nfse status=error, NÃO propaga exceção.
- handle_nfse_webhook_event: INVOICE_AUTHORIZED, INVOICE_ERROR, id desconhecido.
- Integração webhook: INVOICE_AUTHORIZED atualiza linha; PAYMENT_CONFIRMED normal
  sem regressão; com flag OFF nenhuma NFS-e é criada.

Padrão do projeto: FastAPI mínimo + SQLite StaticPool + overrides de get_db.
Mocka _create_asaas_invoice — NUNCA faz HTTP real.
"""
import asyncio
import os
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 — registra todas as tabelas no Base.metadata
from app.core.database import Base, get_db, get_global_db
from app.dependencies.auth import get_current_user
from app.models.nfse import (
    Nfse,
    NFSE_SCHEDULED,
    NFSE_AUTHORIZED,
    NFSE_CANCELED,
    NFSE_ERROR,
    NFSE_SYNCHRONIZED,
)
from app.models.tenant import Tenant
from app.models.user import User
from app.models.payment import Payment
from app.models.tenant_saas_subscription import TenantSaasSubscription, SAAS_ACTIVE, SAAS_OVERDUE
from app.routes import payments as payments_module
from app.services.tenant_seed_service import DEFAULT_TENANT_SLUG

TENANT_ID = "t-nfse"
WEBHOOK_TOKEN = "nfse-webhook-token-123"


# ---------------------------------------------------------------------------
# Helpers de setup
# ---------------------------------------------------------------------------

def _make_db():
    """Cria SQLite in-memory com schema completo + tenant base."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(Tenant(
        id=TENANT_ID,
        name="Tenant NFS-e",
        slug="tenant-nfse",
        status="active",
        plan="pro",
        legal_name="Tenant NFS-e LTDA",
        document_number="11222333000181",
        contact_email="fin@nfse.com",
    ))
    db.commit()
    return db


def _make_payments_client(db):
    """Monta FastAPI mínimo com o router de payments + overrides."""
    app_t = FastAPI()
    app_t.include_router(payments_module.router)
    app_t.dependency_overrides[get_db] = lambda: db
    app_t.dependency_overrides[get_global_db] = lambda: db
    return TestClient(app_t)


def _wh_headers():
    return {"asaas-access-token": WEBHOOK_TOKEN}


# ---------------------------------------------------------------------------
# Fixtures automáticas
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _force_sandbox(monkeypatch):
    monkeypatch.setattr(payments_module, "PAYMENT_MODE", "asaas_sandbox")


@pytest.fixture(autouse=True)
def _set_webhook_token(monkeypatch):
    monkeypatch.setenv("ASAAS_WEBHOOK_TOKEN", WEBHOOK_TOKEN)


# ---------------------------------------------------------------------------
# 1. Flag OFF — noop total
# ---------------------------------------------------------------------------

def test_flag_off_returns_none(monkeypatch):
    """Com NFS_E_ENABLED=false, issue_nfse retorna None e não persiste nada."""
    import app.services.nfse_service as svc

    monkeypatch.setenv("NFS_E_ENABLED", "false")
    db = _make_db()

    http_called = {"count": 0}

    async def _mock_create(payload):
        http_called["count"] += 1
        return {"id": "inv_123"}

    monkeypatch.setattr(svc, "_create_asaas_invoice", _mock_create)

    result = asyncio.run(
        svc.issue_nfse_for_saas_payment(
            db, tenant_id=TENANT_ID, asaas_payment_id="pay_001", value=129.90
        )
    )

    assert result is None
    assert db.query(Nfse).count() == 0
    assert http_called["count"] == 0, "HTTP não deve ser chamado com flag OFF"


def test_flag_off_various_truthy_values(monkeypatch):
    """Verifica que apenas 1/true/yes ligam a flag."""
    import app.services.nfse_config as cfg

    for val in ("false", "False", "FALSE", "0", "no", "off", ""):
        monkeypatch.setenv("NFS_E_ENABLED", val)
        assert cfg.nfse_enabled() is False, f"Esperava False para NFS_E_ENABLED={val!r}"

    for val in ("true", "True", "TRUE", "1", "yes", "YES"):
        monkeypatch.setenv("NFS_E_ENABLED", val)
        assert cfg.nfse_enabled() is True, f"Esperava True para NFS_E_ENABLED={val!r}"


# ---------------------------------------------------------------------------
# 2. Flag ON + mock sucesso
# ---------------------------------------------------------------------------

def test_flag_on_creates_nfse_scheduled(monkeypatch):
    """Com flag ON e Asaas ok, cria Nfse status=scheduled com asaas_invoice_id."""
    import app.services.nfse_service as svc

    monkeypatch.setenv("NFS_E_ENABLED", "true")
    db = _make_db()

    async def _mock_create(payload):
        assert "payment" in payload
        return {"id": "inv_abc", "status": "SCHEDULED"}

    monkeypatch.setattr(svc, "_create_asaas_invoice", _mock_create)

    result = asyncio.run(
        svc.issue_nfse_for_saas_payment(
            db,
            tenant_id=TENANT_ID,
            asaas_payment_id="pay_002",
            value=129.90,
            subscription_id="sub_x",
        )
    )

    assert result is not None
    assert result.status == NFSE_SCHEDULED
    assert result.asaas_invoice_id == "inv_abc"
    assert result.service_type == "saas"
    assert float(result.value) == 129.90
    assert result.tenant_id == TENANT_ID
    assert result.subscription_id == "sub_x"
    assert result.external_reference == "saas:pay_002"
    assert db.query(Nfse).count() == 1


def test_flag_on_idempotent_second_call(monkeypatch):
    """2ª chamada com mesmo asaas_payment_id retorna existente sem novo HTTP."""
    import app.services.nfse_service as svc

    monkeypatch.setenv("NFS_E_ENABLED", "true")
    db = _make_db()

    call_count = {"n": 0}

    async def _mock_create(payload):
        call_count["n"] += 1
        return {"id": "inv_idem"}

    monkeypatch.setattr(svc, "_create_asaas_invoice", _mock_create)

    # 1ª chamada — cria
    r1 = asyncio.run(
        svc.issue_nfse_for_saas_payment(
            db, tenant_id=TENANT_ID, asaas_payment_id="pay_idem", value=99.0
        )
    )
    assert call_count["n"] == 1
    assert r1.status == NFSE_SCHEDULED

    # 2ª chamada — deve retornar a mesma sem HTTP
    r2 = asyncio.run(
        svc.issue_nfse_for_saas_payment(
            db, tenant_id=TENANT_ID, asaas_payment_id="pay_idem", value=99.0
        )
    )
    assert call_count["n"] == 1, "Não deve chamar HTTP novamente"
    assert r2.id == r1.id
    assert db.query(Nfse).count() == 1


def test_idempotent_allows_reissue_after_error(monkeypatch):
    """Após status=error, a emissão pode ser tentada novamente (nova linha)."""
    import app.services.nfse_service as svc

    monkeypatch.setenv("NFS_E_ENABLED", "true")
    db = _make_db()

    # Simula erro na primeira chamada
    call_count = {"n": 0}

    async def _mock_create_fail(payload):
        call_count["n"] += 1
        raise HTTPException(status_code=502, detail="falha simulada")

    monkeypatch.setattr(svc, "_create_asaas_invoice", _mock_create_fail)

    r1 = asyncio.run(
        svc.issue_nfse_for_saas_payment(
            db, tenant_id=TENANT_ID, asaas_payment_id="pay_retry", value=50.0
        )
    )
    assert r1.status == NFSE_ERROR
    assert call_count["n"] == 1

    # Agora mock de sucesso — deve tentar novamente (erro anterior não bloqueia)
    async def _mock_create_ok(payload):
        call_count["n"] += 1
        return {"id": "inv_retry_ok"}

    monkeypatch.setattr(svc, "_create_asaas_invoice", _mock_create_ok)

    r2 = asyncio.run(
        svc.issue_nfse_for_saas_payment(
            db, tenant_id=TENANT_ID, asaas_payment_id="pay_retry", value=50.0
        )
    )
    assert r2.status == NFSE_SCHEDULED
    assert call_count["n"] == 2
    # Deve haver 2 registros (o de erro anterior + o novo)
    assert db.query(Nfse).count() == 2


# ---------------------------------------------------------------------------
# 3. Flag ON + mock que levanta — best-effort, não propaga
# ---------------------------------------------------------------------------

def test_flag_on_asaas_failure_creates_error_record(monkeypatch):
    """Falha na chamada ao Asaas cria Nfse status=error e NÃO propaga exceção."""
    import app.services.nfse_service as svc

    monkeypatch.setenv("NFS_E_ENABLED", "true")
    db = _make_db()

    async def _mock_create_boom(payload):
        raise HTTPException(status_code=502, detail="gateway timeout simulado")

    monkeypatch.setattr(svc, "_create_asaas_invoice", _mock_create_boom)

    # NÃO deve levantar
    result = asyncio.run(
        svc.issue_nfse_for_saas_payment(
            db, tenant_id=TENANT_ID, asaas_payment_id="pay_err", value=200.0
        )
    )

    assert result is not None
    assert result.status == NFSE_ERROR
    assert result.asaas_invoice_id is None
    assert "502" in result.error_message or "gateway" in result.error_message.lower()
    assert float(result.value) == 200.0
    assert db.query(Nfse).count() == 1


def test_flag_on_generic_exception_creates_error_record(monkeypatch):
    """Exceção genérica (não HTTPException) também cria registro de erro."""
    import app.services.nfse_service as svc

    monkeypatch.setenv("NFS_E_ENABLED", "true")
    db = _make_db()

    async def _mock_create_conn_err(payload):
        raise ConnectionError("host unreachable")

    monkeypatch.setattr(svc, "_create_asaas_invoice", _mock_create_conn_err)

    result = asyncio.run(
        svc.issue_nfse_for_saas_payment(
            db, tenant_id=TENANT_ID, asaas_payment_id="pay_conn", value=50.0
        )
    )

    assert result is not None
    assert result.status == NFSE_ERROR
    assert "unreachable" in result.error_message


# ---------------------------------------------------------------------------
# 4. handle_nfse_webhook_event
# ---------------------------------------------------------------------------

def _create_nfse_row(db, asaas_invoice_id="inv_wh"):
    """Helper: cria uma Nfse já com invoice_id para testes de webhook."""
    nfse = Nfse(
        id="nfse-wh-001",
        tenant_id=TENANT_ID,
        asaas_payment_id="pay_wh",
        asaas_invoice_id=asaas_invoice_id,
        service_type="saas",
        status=NFSE_SCHEDULED,
        value=129.90,
    )
    db.add(nfse)
    db.commit()
    return nfse


def test_webhook_invoice_authorized_fills_fields():
    """INVOICE_AUTHORIZED preenche number/pdf/xml/validation_code e muda status."""
    from app.services.nfse_service import handle_nfse_webhook_event

    db = _make_db()
    _create_nfse_row(db)

    invoice_data = {
        "id": "inv_wh",
        "number": "NF-2026-001",
        "pdfUrl": "https://pdf.example.com/nf.pdf",
        "xmlUrl": "https://xml.example.com/nf.xml",
        "validationCode": "VAL-XYZ-123",
        "status": "AUTHORIZED",
    }

    result = handle_nfse_webhook_event(db, "INVOICE_AUTHORIZED", invoice_data)

    assert result is True
    db.expire_all()
    nfse = db.query(Nfse).first()
    assert nfse.status == NFSE_AUTHORIZED
    assert nfse.nfse_number == "NF-2026-001"
    assert nfse.pdf_url == "https://pdf.example.com/nf.pdf"
    assert nfse.xml_url == "https://xml.example.com/nf.xml"
    assert nfse.validation_code == "VAL-XYZ-123"


def test_webhook_invoice_error_sets_error_status():
    """INVOICE_ERROR atualiza status e error_message."""
    from app.services.nfse_service import handle_nfse_webhook_event

    db = _make_db()
    _create_nfse_row(db)

    invoice_data = {
        "id": "inv_wh",
        "error": "Prefeitura rejeitou: CNPJ inativo",
        "status": "ERROR",
    }

    result = handle_nfse_webhook_event(db, "INVOICE_ERROR", invoice_data)

    assert result is True
    db.expire_all()
    nfse = db.query(Nfse).first()
    assert nfse.status == NFSE_ERROR
    assert "CNPJ inativo" in nfse.error_message


def test_webhook_invoice_canceled_sets_canceled_status():
    """INVOICE_CANCELED atualiza status para canceled."""
    from app.services.nfse_service import handle_nfse_webhook_event

    db = _make_db()
    _create_nfse_row(db)

    result = handle_nfse_webhook_event(db, "INVOICE_CANCELED", {"id": "inv_wh"})

    assert result is True
    db.expire_all()
    assert db.query(Nfse).first().status == NFSE_CANCELED


def test_webhook_invoice_synchronized_updates_status():
    """INVOICE_SYNCHRONIZED atualiza para synchronized."""
    from app.services.nfse_service import handle_nfse_webhook_event

    db = _make_db()
    _create_nfse_row(db)

    result = handle_nfse_webhook_event(db, "INVOICE_SYNCHRONIZED", {"id": "inv_wh"})

    assert result is True
    db.expire_all()
    assert db.query(Nfse).first().status == NFSE_SYNCHRONIZED


def test_webhook_unknown_invoice_id_returns_true_noop():
    """ID desconhecido → retorna True sem efeito colateral."""
    from app.services.nfse_service import handle_nfse_webhook_event

    db = _make_db()

    result = handle_nfse_webhook_event(db, "INVOICE_AUTHORIZED", {"id": "inv_nao_existe"})

    assert result is True
    assert db.query(Nfse).count() == 0


def test_webhook_non_invoice_event_is_noop():
    """Evento que não começa com INVOICE é ignorado com True."""
    from app.services.nfse_service import handle_nfse_webhook_event

    db = _make_db()
    _create_nfse_row(db)

    result = handle_nfse_webhook_event(db, "PAYMENT_CONFIRMED", {"id": "inv_wh"})

    assert result is True
    db.expire_all()
    # Status não deve ter mudado
    assert db.query(Nfse).first().status == NFSE_SCHEDULED


def test_webhook_empty_invoice_data_is_noop():
    """invoice_data vazio → noop, retorna True."""
    from app.services.nfse_service import handle_nfse_webhook_event

    db = _make_db()
    result = handle_nfse_webhook_event(db, "INVOICE_AUTHORIZED", {})
    assert result is True


# ---------------------------------------------------------------------------
# 5. Integração webhook HTTP
# ---------------------------------------------------------------------------

def test_webhook_invoice_authorized_via_http_updates_nfse(monkeypatch):
    """Evento INVOICE_AUTHORIZED via POST /payments/webhooks/asaas atualiza Nfse."""
    monkeypatch.setenv("ASAAS_WEBHOOK_TOKEN", WEBHOOK_TOKEN)
    db = _make_db()

    # Cria linha de NFS-e com status=scheduled aguardando confirmação
    nfse = Nfse(
        id="nfse-http-001",
        tenant_id=TENANT_ID,
        asaas_payment_id="pay_http",
        asaas_invoice_id="inv_http_001",
        service_type="saas",
        status=NFSE_SCHEDULED,
        value=129.90,
    )
    db.add(nfse)
    db.commit()

    client = _make_payments_client(db)
    payload = {
        "event": "INVOICE_AUTHORIZED",
        "invoice": {
            "id": "inv_http_001",
            "number": "NF-HTTP-001",
            "pdfUrl": "https://pdf.url/nf.pdf",
            "xmlUrl": "https://xml.url/nf.xml",
            "validationCode": "VAL-HTTP",
        },
    }

    r = client.post("/payments/webhooks/asaas", json=payload, headers=_wh_headers())
    assert r.status_code == 200, r.text
    assert r.json().get("ok") is True

    db.expire_all()
    nfse_updated = db.get(Nfse, "nfse-http-001")
    assert nfse_updated.status == NFSE_AUTHORIZED
    assert nfse_updated.nfse_number == "NF-HTTP-001"


def test_payment_confirmed_webhook_no_regression(monkeypatch):
    """PAYMENT_CONFIRMED continua funcionando normalmente (zero regressão)."""
    monkeypatch.setenv("ASAAS_WEBHOOK_TOKEN", WEBHOOK_TOKEN)
    db = _make_db()

    # Cria um Payment local
    db.add(User(id="u1", email="t@t.com", password_hash="x", role="cliente", tenant_id=TENANT_ID))
    db.add(Payment(
        id="pay-local-001",
        tenant_id=TENANT_ID,
        tutor_id="u1",
        walk_id=None,
        amount=50.0,
        status="pagamento_sandbox_criado",
        provider="asaas_sandbox",
        provider_payment_id="asaas_pay_001",
    ))
    db.commit()

    client = _make_payments_client(db)
    payload = {
        "event": "PAYMENT_CONFIRMED",
        "payment": {
            "id": "asaas_pay_001",
            "status": "CONFIRMED",
            "externalReference": "some-ref",
        },
    }

    r = client.post("/payments/webhooks/asaas", json=payload, headers=_wh_headers())
    assert r.status_code == 200, r.text

    db.expire_all()
    pay = db.get(Payment, "pay-local-001")
    assert pay.status == "pagamento_confirmado_sandbox"


def test_invoice_webhook_with_flag_off_creates_no_nfse_on_tenant_sub(monkeypatch):
    """PAYMENT_RECEIVED de mensalidade SaaS com flag OFF não cria NFS-e."""
    monkeypatch.setenv("ASAAS_WEBHOOK_TOKEN", WEBHOOK_TOKEN)
    monkeypatch.setenv("NFS_E_ENABLED", "false")

    db = _make_db()
    sub = TenantSaasSubscription(
        tenant_id=TENANT_ID,
        plan="pro",
        price=129.90,
        status=SAAS_OVERDUE,
        overdue_since=None,
        asaas_subscription_id="as_nfse_test",
    )
    db.add(sub)
    db.commit()

    client = _make_payments_client(db)
    payload = {
        "event": "PAYMENT_RECEIVED",
        "payment": {
            "id": "p_saas_nfse",
            "status": "RECEIVED",
            "externalReference": f"tenant_sub:{sub.id}",
            "subscription": "as_nfse_test",
            "value": 129.90,
        },
    }

    r = client.post("/payments/webhooks/asaas", json=payload, headers=_wh_headers())
    assert r.status_code == 200, r.text

    # Com flag OFF, zero NFS-e criada
    assert db.query(Nfse).count() == 0


def test_invoice_event_bad_invoice_field_returns_400(monkeypatch):
    """Campo 'invoice' não-dict deve retornar 400."""
    monkeypatch.setenv("ASAAS_WEBHOOK_TOKEN", WEBHOOK_TOKEN)
    db = _make_db()
    client = _make_payments_client(db)

    payload = {
        "event": "INVOICE_AUTHORIZED",
        "invoice": 99999,  # não é dict
    }

    r = client.post("/payments/webhooks/asaas", json=payload, headers=_wh_headers())
    assert r.status_code == 400
    assert "invoice" in r.json()["detail"].lower()


def test_invoice_event_without_invoice_field_is_noop(monkeypatch):
    """Evento INVOICE_* sem campo 'invoice' no payload → noop seguro (200)."""
    monkeypatch.setenv("ASAAS_WEBHOOK_TOKEN", WEBHOOK_TOKEN)
    db = _make_db()
    client = _make_payments_client(db)

    payload = {"event": "INVOICE_CREATED"}  # sem campo invoice

    r = client.post("/payments/webhooks/asaas", json=payload, headers=_wh_headers())
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# 6. _build_invoice_payload unit tests
# ---------------------------------------------------------------------------

def test_build_invoice_payload_contains_required_fields(monkeypatch):
    """_build_invoice_payload sempre inclui payment, value, serviceDescription, effectiveDate."""
    from app.services.nfse_service import _build_invoice_payload
    import app.services.nfse_config as cfg

    monkeypatch.setenv("NFSE_MUNICIPAL_SERVICE_CODE", "")
    monkeypatch.setenv("NFSE_ISS_RATE", "0.0")
    monkeypatch.setenv("NFSE_SERVICE_DESCRIPTION", "Descricao teste")

    payload = _build_invoice_payload(
        asaas_payment_id="pay_build", value=99.0, service_type="saas"
    )

    assert payload["payment"] == "pay_build"
    assert payload["value"] == 99.0
    assert payload["serviceDescription"] == "Descricao teste"
    assert "effectiveDate" in payload


def test_build_invoice_payload_with_iss_rate(monkeypatch):
    """Com NFSE_ISS_RATE > 0, inclui taxes no payload."""
    from app.services.nfse_service import _build_invoice_payload

    monkeypatch.setenv("NFSE_ISS_RATE", "2.5")
    monkeypatch.setenv("NFSE_MUNICIPAL_SERVICE_CODE", "1.07")

    payload = _build_invoice_payload(
        asaas_payment_id="pay_iss", value=129.90, service_type="saas"
    )

    assert "taxes" in payload
    assert payload.get("municipalServiceCode") == "1.07"


def test_build_invoice_payload_without_iss_rate(monkeypatch):
    """Com NFSE_ISS_RATE = 0, NÃO inclui taxes no payload."""
    from app.services.nfse_service import _build_invoice_payload

    monkeypatch.setenv("NFSE_ISS_RATE", "0.0")
    monkeypatch.setenv("NFSE_MUNICIPAL_SERVICE_CODE", "")

    payload = _build_invoice_payload(
        asaas_payment_id="pay_no_iss", value=50.0, service_type="saas"
    )

    assert "taxes" not in payload
    assert "municipalServiceCode" not in payload
