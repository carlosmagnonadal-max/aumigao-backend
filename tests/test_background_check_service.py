"""BG-1.5 — background_check_service: status agregado + links oficiais + flag.

Usa um stub simples de profile/certificate (o servico nao toca banco).
"""
from datetime import datetime
from types import SimpleNamespace

from app.services import background_check_service as svc
from app.services.tenant_feature_runtime_service import (
    PRODUCT_RUNTIME_FEATURE_KEYS,
    get_default_feature_runtime,
)


def _profile():
    return SimpleNamespace(background_check_status="none", background_verified_at=None)


def _cert(cert_type, status, uf=None):
    return SimpleNamespace(cert_type=cert_type, status=status, issuer_uf=uf)


# ----------------------------------------------------------------- status agregado
def test_status_none_when_no_certs():
    p = _profile()
    assert svc.compute_background_status(p, []) == "none"
    assert p.background_check_status == "none"


def test_status_submitted_when_required_pending():
    p = _profile()
    certs = [_cert("pf", "pending"), _cert("tj", "pending", uf="SP")]
    assert svc.compute_background_status(p, certs) == "submitted"
    assert p.background_check_status == "submitted"


def test_status_partial_when_one_required_validated():
    p = _profile()
    certs = [_cert("pf", "validated"), _cert("tj", "pending", uf="SP")]
    assert svc.compute_background_status(p, certs) == "partial"


def test_status_verified_when_both_required_validated():
    p = _profile()
    certs = [_cert("pf", "validated"), _cert("tj", "validated", uf="SP")]
    assert svc.compute_background_status(p, certs) == "verified"
    assert p.background_check_status == "verified"
    assert isinstance(p.background_verified_at, datetime)


def test_status_flagged_when_any_required_rejected():
    p = _profile()
    certs = [_cert("pf", "validated"), _cert("tj", "rejected", uf="SP")]
    assert svc.compute_background_status(p, certs) == "flagged"
    assert p.background_check_status == "flagged"


def test_verified_then_demoted_clears_verified_at():
    p = _profile()
    svc.compute_background_status(p, [_cert("pf", "validated"), _cert("tj", "validated", uf="SP")])
    assert p.background_verified_at is not None
    # Uma certidao expira/rejeita depois -> deixa de ser verified -> limpa o carimbo.
    svc.compute_background_status(p, [_cert("pf", "validated"), _cert("tj", "rejected", uf="SP")])
    assert p.background_verified_at is None


def test_complementary_certs_do_not_break_required_logic():
    p = _profile()
    certs = [
        _cert("pf", "validated"),
        _cert("tj", "validated", uf="SP"),
        _cert("trf", "pending", uf="TRF1"),
        _cert("tse", "pending"),
    ]
    assert svc.compute_background_status(p, certs) == "verified"


# ------------------------------------------------------------------- links oficiais
def test_official_url_pf():
    assert svc.official_validation_url("pf") == "https://servicos.pf.gov.br/epol-sinic-publico/validar-cac"


def test_official_url_tse():
    assert "tse.jus.br" in svc.official_validation_url("tse")


def test_official_url_tj_known_uf():
    assert svc.official_validation_url("tj", "SP") == "https://www.tjsp.jus.br/Certidao"
    assert "tjrj" in svc.official_validation_url("tj", "RJ")


def test_official_url_tj_fallback():
    url = svc.official_validation_url("tj", "AC")
    assert url and url.startswith("https://")


def test_official_url_trf_fallback_and_region():
    assert "trf1" in svc.official_validation_url("trf", "TRF1")
    assert svc.official_validation_url("trf", "ZZ").startswith("https://")


# --------------------------------------------------------------------------- flag
def test_background_checks_in_product_runtime_keys():
    assert "background_checks" in PRODUCT_RUNTIME_FEATURE_KEYS


def test_background_checks_default_off():
    assert get_default_feature_runtime()["background_checks"] is False
