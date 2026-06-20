"""
TDD — testes de unidade para app.core.pii_crypto.

Cobertura:
- encrypt/decrypt roundtrip
- passthrough de "" e None
- decrypt tolerante a texto puro legado (InvalidToken)
- blind_index determinístico e normalizado
- blind_index(None/"") == None
- is_encrypted: True para token Fernet, False para texto puro
"""

import os
import pytest

# A PII_ENCRYPTION_KEY de teste é setada pelo conftest.py antes de qualquer import.
from app.core.pii_crypto import (
    blind_index,
    decrypt,
    encrypt,
    is_encrypted,
    normalize_cpf,
    _fernet,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _ensure_key(monkeypatch):
    """Garante que a chave de teste está definida e o cache está limpo."""
    monkeypatch.setenv("PII_ENCRYPTION_KEY", "sI9VJYXwVrM29Mykh649L9MzxjbneiYu3dI9X6k29ws=")
    _fernet.cache_clear()
    yield
    _fernet.cache_clear()


# ---------------------------------------------------------------------------
# normalize_cpf
# ---------------------------------------------------------------------------


def test_normalize_cpf_removes_punctuation():
    assert normalize_cpf("123.456.789-00") == "12345678900"


def test_normalize_cpf_keeps_digits():
    assert normalize_cpf("12345678900") == "12345678900"


def test_normalize_cpf_empty():
    assert normalize_cpf("") == ""


def test_normalize_cpf_none():
    assert normalize_cpf(None) == ""


# ---------------------------------------------------------------------------
# encrypt / decrypt — roundtrip
# ---------------------------------------------------------------------------


def test_encrypt_decrypt_roundtrip():
    plaintext = "12345678900"
    token = encrypt(plaintext)
    assert token != plaintext  # deve ser diferente (cifrado)
    assert decrypt(token) == plaintext


def test_encrypt_decrypt_with_formatted_cpf():
    plaintext = "123.456.789-00"
    token = encrypt(plaintext)
    assert decrypt(token) == plaintext


def test_encrypt_produces_fernet_token():
    token = encrypt("99999999999")
    # Tokens Fernet começam com gAAAA (base64 de versão + timestamp)
    assert token.startswith("gAAAA")


# ---------------------------------------------------------------------------
# Passthrough de vazio
# ---------------------------------------------------------------------------


def test_encrypt_empty_string_passthrough():
    assert encrypt("") == ""


def test_encrypt_none_passthrough():
    assert encrypt(None) is None


def test_decrypt_empty_string_passthrough():
    assert decrypt("") == ""


def test_decrypt_none_passthrough():
    assert decrypt(None) is None


# ---------------------------------------------------------------------------
# decrypt tolerante — texto puro legado
# ---------------------------------------------------------------------------


def test_decrypt_plain_text_returns_original():
    """Texto puro que não é um token Fernet válido deve ser retornado inalterado."""
    plain = "12345678900"
    result = decrypt(plain)
    assert result == plain


def test_decrypt_formatted_cpf_plain_text_returns_original():
    plain = "123.456.789-00"
    assert decrypt(plain) == plain


def test_decrypt_legacy_rg_plain_text_returns_original():
    plain = "MG-12.345.678"
    assert decrypt(plain) == plain


# ---------------------------------------------------------------------------
# is_encrypted
# ---------------------------------------------------------------------------


def test_is_encrypted_true_for_fernet_token():
    token = encrypt("12345678900")
    assert is_encrypted(token) is True


def test_is_encrypted_false_for_plain_text():
    assert is_encrypted("12345678900") is False


def test_is_encrypted_false_for_empty():
    assert is_encrypted("") is False


def test_is_encrypted_false_for_formatted_cpf():
    assert is_encrypted("123.456.789-00") is False


# ---------------------------------------------------------------------------
# blind_index — determinístico e normalizado
# ---------------------------------------------------------------------------


def test_blind_index_deterministic():
    """Mesma entrada → mesmo índice sempre."""
    idx1 = blind_index("12345678900")
    idx2 = blind_index("12345678900")
    assert idx1 == idx2


def test_blind_index_normalizes_cpf():
    """CPF com e sem pontuação produzem o mesmo índice."""
    idx_plain = blind_index("12345678900")
    idx_formatted = blind_index("123.456.789-00")
    assert idx_plain == idx_formatted
    assert idx_plain is not None


def test_blind_index_different_cpfs_produce_different_indexes():
    idx1 = blind_index("12345678900")
    idx2 = blind_index("98765432100")
    assert idx1 != idx2


def test_blind_index_returns_hex_string():
    idx = blind_index("12345678900")
    assert isinstance(idx, str)
    # HMAC-SHA256 hexdigest = 64 caracteres hexadecimais
    assert len(idx) == 64
    assert all(c in "0123456789abcdef" for c in idx)


def test_blind_index_none_returns_none():
    assert blind_index(None) is None


def test_blind_index_empty_returns_none():
    assert blind_index("") is None


def test_blind_index_only_punctuation_returns_none():
    """CPF composto só de pontuação (sem dígitos) → None."""
    assert blind_index("...---") is None


# ---------------------------------------------------------------------------
# Erro de chave ausente
# ---------------------------------------------------------------------------


def test_fernet_raises_runtime_error_without_key(monkeypatch):
    monkeypatch.delenv("PII_ENCRYPTION_KEY", raising=False)
    _fernet.cache_clear()
    with pytest.raises(RuntimeError, match="PII_ENCRYPTION_KEY"):
        _fernet()
    _fernet.cache_clear()


def test_blind_index_raises_runtime_error_without_key(monkeypatch):
    monkeypatch.delenv("PII_ENCRYPTION_KEY", raising=False)
    _fernet.cache_clear()
    with pytest.raises(RuntimeError, match="PII_ENCRYPTION_KEY"):
        blind_index("12345678900")
    _fernet.cache_clear()
