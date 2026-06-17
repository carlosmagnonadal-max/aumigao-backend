"""BG-2 — rotas do passeador: consent + certificate + status.

Reusa o build minimo de tests/test_routes_walker_core.py.
"""
from tests.test_routes_walker_core import build, WALKER_ID
from app.models.walker_profile import WalkerProfile
from app.models.walker_background_certificate import WalkerBackgroundCertificate


def test_certificate_without_consent_returns_400():
    client, _ = build()
    r = client.post(
        "/walker/background/certificate",
        json={"cert_type": "pf", "cert_number": "12345"},
    )
    assert r.status_code == 400, r.text
    assert "consentimento" in r.json()["detail"].lower()


def test_consent_then_certificate_creates_row():
    client, db = build()
    rc = client.post("/walker/background/consent", json={"consent_version": "v1"})
    assert rc.status_code == 200, rc.text
    assert rc.json()["consent_version"] == "v1"
    profile = db.query(WalkerProfile).filter(WalkerProfile.user_id == WALKER_ID).first()
    assert profile.background_consent_at is not None

    r = client.post(
        "/walker/background/certificate",
        json={"cert_type": "pf", "cert_number": "PF-123", "document_url": "https://x/uploads/walker-documents/o/background-background_pf-abc.pdf"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["certificate"]["cert_type"] == "pf"
    assert body["certificate"]["cert_number"] == "PF-123"
    assert body["certificate"]["status"] == "pending"
    assert "servicos.pf.gov.br" in body["certificate"]["official_validation_url"]
    assert body["background_check_status"] == "submitted"

    rows = db.query(WalkerBackgroundCertificate).all()
    assert len(rows) == 1
    assert rows[0].cert_type == "pf"


def test_certificate_resubmit_updates_same_row_and_resets_pending():
    client, db = build()
    client.post("/walker/background/consent", json={})
    client.post("/walker/background/certificate", json={"cert_type": "tj", "uf": "sp", "cert_number": "TJ-1"})
    # admin valida manualmente (simulado direto no banco)
    row = db.query(WalkerBackgroundCertificate).first()
    row.status = "validated"
    db.commit()
    # passeador reenvia -> volta a pending, mesma linha
    client.post("/walker/background/certificate", json={"cert_type": "tj", "uf": "RJ", "cert_number": "TJ-2"})
    rows = db.query(WalkerBackgroundCertificate).all()
    assert len(rows) == 1
    assert rows[0].cert_number == "TJ-2"
    assert rows[0].issuer_uf == "RJ"
    assert rows[0].status == "pending"


def test_get_background_returns_aggregate_and_certs():
    client, _ = build()
    client.post("/walker/background/consent", json={})
    client.post("/walker/background/certificate", json={"cert_type": "pf", "cert_number": "PF-1"})
    r = client.get("/walker/background")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["background_check_status"] == "submitted"
    assert len(body["certificates"]) == 1
    assert body["consent_version"] == "v1"


def test_invalid_cert_type_400():
    client, _ = build()
    client.post("/walker/background/consent", json={})
    r = client.post("/walker/background/certificate", json={"cert_type": "xx", "cert_number": "1"})
    assert r.status_code == 400


def test_empty_cert_number_400():
    client, _ = build()
    client.post("/walker/background/consent", json={})
    r = client.post("/walker/background/certificate", json={"cert_type": "pf", "cert_number": "  "})
    assert r.status_code == 400
