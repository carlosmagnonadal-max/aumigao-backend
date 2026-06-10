"""Unit tests for app/core/security.py.

Pure functions (password hashing + JWT minting) with no DB. The module reads
JWT_SECRET from the loaded .env at import time, so importing it works in this
repo (a valid 41-char secret is present).
"""
from datetime import datetime, timedelta, timezone

import jwt
import pytest

from app.core import security


# --------------------------------------------------------------------------
# get_password_hash + verify_password
# --------------------------------------------------------------------------

def test_hash_round_trip():
    h = security.get_password_hash("s3nha-forte")
    assert security.verify_password("s3nha-forte", h) is True


def test_hash_format_is_pbkdf2_sha256_with_three_parts():
    h = security.get_password_hash("abc")
    parts = h.split("$")
    assert len(parts) == 3
    algorithm, salt_hex, digest_hex = parts
    assert algorithm == "pbkdf2_sha256"
    # salt is 16 random bytes -> 32 hex chars
    assert len(salt_hex) == 32
    bytes.fromhex(salt_hex)  # valid hex
    bytes.fromhex(digest_hex)  # valid hex
    # sha256 digest -> 32 bytes -> 64 hex chars
    assert len(digest_hex) == 64


def test_hash_is_salted_unique_per_call():
    a = security.get_password_hash("samepw")
    b = security.get_password_hash("samepw")
    assert a != b  # random salt makes hashes differ
    # ...but both verify against the original password
    assert security.verify_password("samepw", a)
    assert security.verify_password("samepw", b)


def test_verify_wrong_password_returns_false():
    h = security.get_password_hash("correct-horse")
    assert security.verify_password("wrong-horse", h) is False


def test_verify_empty_password_round_trip():
    h = security.get_password_hash("")
    assert security.verify_password("", h) is True
    assert security.verify_password("x", h) is False


def test_verify_unicode_password_round_trip():
    pw = "señha-com-acentuação-日本語"
    h = security.get_password_hash(pw)
    assert security.verify_password(pw, h) is True
    assert security.verify_password("senha-com-acentuacao", h) is False


@pytest.mark.parametrize(
    "bad_hash",
    [
        "",                       # empty -> split yields 1 part -> unpack error -> False
        "nodollarsigns",          # no '$' -> unpack error -> False
        "onlyone$part",           # only 2 parts -> unpack error -> False
        "pbkdf2_sha256$deadbeef", # 2 parts after algo -> still unpack error -> False
    ],
)
def test_verify_malformed_hash_returns_false(bad_hash):
    assert security.verify_password("anything", bad_hash) is False


def test_verify_unknown_algorithm_returns_false():
    # well-formed 3-part hash but unsupported algorithm prefix
    h = security.get_password_hash("pw")
    _, salt_hex, digest_hex = h.split("$")
    tampered = f"bcrypt${salt_hex}${digest_hex}"
    assert security.verify_password("pw", tampered) is False


def test_verify_non_hex_salt_returns_false():
    # bytes.fromhex raises ValueError -> caught -> False
    bad = "pbkdf2_sha256$ZZZZ$deadbeef"
    assert security.verify_password("pw", bad) is False


def test_verify_tampered_digest_returns_false():
    h = security.get_password_hash("pw")
    algorithm, salt_hex, digest_hex = h.split("$")
    # flip the last hex char of the digest
    flipped = "0" if digest_hex[-1] != "0" else "1"
    tampered = f"{algorithm}${salt_hex}${digest_hex[:-1]}{flipped}"
    assert security.verify_password("pw", tampered) is False


def test_verify_extra_dollar_in_digest_is_tolerated_by_split_maxsplit():
    # split('$', 2) means the third segment may contain extra '$';
    # such a digest is not valid hex so verification must be False, not raise.
    h = security.get_password_hash("pw")
    algorithm, salt_hex, digest_hex = h.split("$")
    weird = f"{algorithm}${salt_hex}${digest_hex}$extra"
    assert security.verify_password("pw", weird) is False


# --------------------------------------------------------------------------
# create_access_token + decode
# --------------------------------------------------------------------------

def _decode(token: str) -> dict:
    return jwt.decode(token, security.SECRET_KEY, algorithms=[security.ALGORITHM])


def test_create_access_token_sets_sub():
    token = security.create_access_token("user-123")
    payload = _decode(token)
    assert payload["sub"] == "user-123"


def test_create_access_token_sets_exp_in_future_per_config():
    before = datetime.now(timezone.utc)
    token = security.create_access_token("u")
    after = datetime.now(timezone.utc)
    payload = _decode(token)
    exp = datetime.fromtimestamp(payload["exp"], tz=timezone.utc)
    expected_low = before + timedelta(minutes=security.ACCESS_TOKEN_EXPIRE_MINUTES)
    expected_high = after + timedelta(minutes=security.ACCESS_TOKEN_EXPIRE_MINUTES)
    # exp is rounded to whole seconds by JWT encoding; allow a small slack.
    assert expected_low - timedelta(seconds=2) <= exp <= expected_high + timedelta(seconds=2)


def test_create_access_token_merges_extra_claims():
    token = security.create_access_token("u", extra={"role": "admin", "tenant_id": "t1"})
    payload = _decode(token)
    assert payload["sub"] == "u"
    assert payload["role"] == "admin"
    assert payload["tenant_id"] == "t1"


def test_extra_claims_can_override_sub():
    # create_access_token does payload.update(extra), so extra wins on collisions.
    token = security.create_access_token("original", extra={"sub": "overridden"})
    payload = _decode(token)
    assert payload["sub"] == "overridden"


def test_extra_none_produces_only_sub_and_exp():
    token = security.create_access_token("u", extra=None)
    payload = _decode(token)
    assert set(payload.keys()) == {"sub", "exp"}


def test_token_uses_configured_algorithm():
    token = security.create_access_token("u")
    header = jwt.get_unverified_header(token)
    assert header["alg"] == security.ALGORITHM == "HS256"


def test_decode_with_wrong_secret_fails():
    token = security.create_access_token("u")
    with pytest.raises(jwt.InvalidSignatureError):
        jwt.decode(token, "wrong-secret-but-long-enough-xxxxxxxxxxx", algorithms=[security.ALGORITHM])


def test_decode_expired_token_raises(monkeypatch):
    # Force the token to be minted in the past so it is already expired.
    fixed_past = datetime(2000, 1, 1, tzinfo=timezone.utc)

    class _FakeDateTime:
        @staticmethod
        def now(tz=None):
            return fixed_past

    monkeypatch.setattr(security, "datetime", _FakeDateTime)
    token = security.create_access_token("u")
    with pytest.raises(jwt.ExpiredSignatureError):
        _decode(token)
