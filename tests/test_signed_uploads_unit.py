"""Testes de unidade focados para app/services/signed_uploads.py.

NOTA: tests/test_signed_uploads.py ja existe (commitado, estilo integracao com
FastAPI/DB). Para nao destruir esse trabalho commitado, estes testes de unidade
puros ficam em arquivo separado de nome unico. Cobrem o comportamento real de:
  - normalize_upload_path (path traversal/.., prefixo uploads/, backslashes)
  - is_sensitive_upload_path
  - create_signed_upload_url + has_valid_upload_signature (HMAC valido/
    invalido/expirado), controlando o tempo via monkeypatch.

Servico puro -> sem DB, sem app.main, sem alembic.
"""

from urllib.parse import urlsplit, parse_qs

import pytest

from app.services import signed_uploads as su


@pytest.fixture(autouse=True)
def fixed_secret(monkeypatch):
    """Fixa o segredo de assinatura para todos os testes deste modulo."""
    monkeypatch.setenv("SIGNED_UPLOAD_SECRET", "test-secret-for-signed-uploads-32chars")
    return "test-secret-for-signed-uploads-32chars"


# --- normalize_upload_path -------------------------------------------------

def test_normalize_simple_path():
    assert su.normalize_upload_path("walker-documents/abc/file.png") == (
        "walker-documents/abc/file.png"
    )


def test_normalize_strips_leading_slashes():
    assert su.normalize_upload_path("///walker-documents/x/file.png") == (
        "walker-documents/x/file.png"
    )


def test_normalize_removes_uploads_prefix():
    assert su.normalize_upload_path("uploads/walker-documents/x/f.png") == (
        "walker-documents/x/f.png"
    )


def test_normalize_converts_backslashes():
    assert su.normalize_upload_path("walker-documents\\x\\f.png") == (
        "walker-documents/x/f.png"
    )


def test_normalize_collapses_empty_segments():
    assert su.normalize_upload_path("a//b///c.png") == "a/b/c.png"


def test_normalize_rejects_dotdot_traversal():
    assert su.normalize_upload_path("walker-documents/../secret.png") is None


def test_normalize_rejects_single_dot():
    assert su.normalize_upload_path("walker-documents/./f.png") is None


def test_normalize_rejects_leading_dotdot():
    assert su.normalize_upload_path("../etc/passwd") is None


def test_normalize_empty_returns_none():
    assert su.normalize_upload_path("") is None


def test_normalize_none_returns_none():
    assert su.normalize_upload_path(None) is None


def test_normalize_only_slashes_returns_none():
    assert su.normalize_upload_path("////") is None


def test_normalize_just_uploads_prefix_returns_none():
    assert su.normalize_upload_path("uploads/") is None


# --- is_sensitive_upload_path ----------------------------------------------

def test_sensitive_identity_front_is_sensitive():
    assert su.is_sensitive_upload_path(
        "walker-documents/walker-1/identity_front-123.png"
    ) is True


def test_sensitive_all_prefixes():
    for prefix in (
        "identity_front-",
        "identity_back-",
        "address_proof-",
        "selfie-",
    ):
        path = f"walker-documents/walker-1/{prefix}x.png"
        assert su.is_sensitive_upload_path(path) is True


def test_sensitive_wrong_root_dir_not_sensitive():
    assert su.is_sensitive_upload_path(
        "other-folder/walker-1/identity_front-1.png"
    ) is False


def test_sensitive_non_matching_filename_not_sensitive():
    assert su.is_sensitive_upload_path(
        "walker-documents/walker-1/profile_photo-1.png"
    ) is False


def test_sensitive_too_few_parts_not_sensitive():
    assert su.is_sensitive_upload_path(
        "walker-documents/identity_front-1.png"
    ) is False


def test_sensitive_invalid_path_not_sensitive():
    assert su.is_sensitive_upload_path("../../identity_front-1.png") is False


def test_sensitive_deep_nesting_uses_last_part_as_filename():
    assert su.is_sensitive_upload_path(
        "walker-documents/a/b/c/selfie-9.jpg"
    ) is True


def test_sensitive_prefix_in_middle_not_filename():
    assert su.is_sensitive_upload_path(
        "walker-documents/identity_front-1/normal.png"
    ) is False


# --- create_signed_upload_url ----------------------------------------------

def test_create_signed_url_none_passthrough():
    assert su.create_signed_upload_url(None) is None


def test_create_signed_url_empty_passthrough():
    assert su.create_signed_upload_url("") == ""


def test_create_signed_url_without_uploads_marker_passthrough():
    url = "https://cdn.example.com/static/logo.png"
    assert su.create_signed_upload_url(url) == url


def test_create_signed_url_non_sensitive_passthrough():
    url = "https://cdn.example.com/uploads/walker-documents/w1/profile_photo-1.png"
    assert su.create_signed_upload_url(url) == url


def test_create_signed_url_sensitive_adds_signature(monkeypatch):
    monkeypatch.setattr(su.time, "time", lambda: 1_000_000.0)
    url = "https://cdn.example.com/uploads/walker-documents/w1/selfie-5.png"
    signed = su.create_signed_upload_url(url, ttl_seconds=600)
    split = urlsplit(signed)
    params = parse_qs(split.query)
    assert params["expires"][0] == str(1_000_000 + 600)
    assert "signature" in params
    assert split.path == "/uploads/walker-documents/w1/selfie-5.png"
    assert split.scheme == "https"
    assert split.netloc == "cdn.example.com"


def test_create_signed_url_then_validates(monkeypatch):
    monkeypatch.setattr(su.time, "time", lambda: 2_000_000.0)
    url = "https://cdn.example.com/uploads/walker-documents/w1/identity_back-9.png"
    signed = su.create_signed_upload_url(url, ttl_seconds=600)
    split = urlsplit(signed)
    upload_path = split.path.split("/uploads/", 1)[1]
    assert su.has_valid_upload_signature(upload_path, split.query) is True


def test_create_signed_url_path_only_no_scheme(monkeypatch):
    monkeypatch.setattr(su.time, "time", lambda: 3_000_000.0)
    url = "/uploads/walker-documents/w1/address_proof-1.pdf"
    signed = su.create_signed_upload_url(url)
    assert "signature=" in signed and "expires=" in signed


# --- has_valid_upload_signature --------------------------------------------

def _build_query(upload_path, expires, signature=None):
    sig = signature if signature is not None else su._signature(upload_path, expires)
    return f"expires={expires}&signature={sig}"


def test_valid_signature_passes(monkeypatch):
    monkeypatch.setattr(su.time, "time", lambda: 100.0)
    path = "walker-documents/w1/selfie-1.png"
    query = _build_query(path, 700)
    assert su.has_valid_upload_signature(path, query) is True


def test_valid_signature_accepts_bytes_query(monkeypatch):
    monkeypatch.setattr(su.time, "time", lambda: 100.0)
    path = "walker-documents/w1/selfie-1.png"
    query = _build_query(path, 700).encode("utf-8")
    assert su.has_valid_upload_signature(path, query) is True


def test_invalid_signature_fails(monkeypatch):
    monkeypatch.setattr(su.time, "time", lambda: 100.0)
    path = "walker-documents/w1/selfie-1.png"
    query = _build_query(path, 700, signature="deadbeef")
    assert su.has_valid_upload_signature(path, query) is False


def test_expired_signature_fails(monkeypatch):
    monkeypatch.setattr(su.time, "time", lambda: 1000.0)
    path = "walker-documents/w1/selfie-1.png"
    query = _build_query(path, 500)  # expires < now
    assert su.has_valid_upload_signature(path, query) is False


def test_expires_exactly_now_is_valid(monkeypatch):
    # Condicao real e "expires < now"; igual -> nao expirado.
    monkeypatch.setattr(su.time, "time", lambda: 1000.0)
    path = "walker-documents/w1/selfie-1.png"
    query = _build_query(path, 1000)
    assert su.has_valid_upload_signature(path, query) is True


def test_missing_expires_fails():
    path = "walker-documents/w1/selfie-1.png"
    assert su.has_valid_upload_signature(path, "signature=abc") is False


def test_missing_signature_fails(monkeypatch):
    monkeypatch.setattr(su.time, "time", lambda: 100.0)
    path = "walker-documents/w1/selfie-1.png"
    assert su.has_valid_upload_signature(path, "expires=700") is False


def test_non_integer_expires_fails():
    path = "walker-documents/w1/selfie-1.png"
    query = "expires=notanumber&signature=abc"
    assert su.has_valid_upload_signature(path, query) is False


def test_invalid_path_fails():
    query = "expires=99999999999&signature=abc"
    assert su.has_valid_upload_signature("../etc/passwd", query) is False


def test_signature_bound_to_path(monkeypatch):
    monkeypatch.setattr(su.time, "time", lambda: 100.0)
    path_a = "walker-documents/w1/selfie-1.png"
    path_b = "walker-documents/w1/selfie-2.png"
    query = _build_query(path_a, 700)
    assert su.has_valid_upload_signature(path_b, query) is False


def test_signature_validates_with_uploads_prefix_input(monkeypatch):
    # has_valid normaliza a entrada -> prefixo "uploads/" tambem valida.
    monkeypatch.setattr(su.time, "time", lambda: 100.0)
    normalized = "walker-documents/w1/selfie-1.png"
    query = _build_query(normalized, 700)
    assert su.has_valid_upload_signature("uploads/" + normalized, query) is True
