"""Tests for the PBKDF2 → bcrypt migration in app/core/security.py.

Acceptance criteria:
1. get_password_hash produces a $2b$ hash; verify_password accepts the right
   password and rejects the wrong one.
2. ANTI-LOCKOUT: a hash produced with the OLD pbkdf2_sha256 algorithm still
   verifies True with the correct password and False with a wrong password.
3. password_needs_rehash returns True for legacy pbkdf2 hashes and False for
   a fresh bcrypt hash.
4. Passwords longer than 72 bytes and multibyte (emoji/accented) passwords
   hash and verify correctly (no silent truncation).
5. Empty string / garbage hash → verify returns False, never raises.
"""
import base64
import hashlib
from hashlib import pbkdf2_hmac
from os import urandom

import pytest

from app.core.security import (
    _BCRYPT_COST,
    _bcrypt_prepare,
    get_password_hash,
    password_needs_rehash,
    verify_password,
)


# ---------------------------------------------------------------------------
# Helper: produce a legacy pbkdf2_sha256 hash exactly as the old code did
# ---------------------------------------------------------------------------

def _legacy_pbkdf2_hash(password: str) -> str:
    """Reproduce the old get_password_hash algorithm inline."""
    salt = urandom(16)
    digest = pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 120_000)
    return f"pbkdf2_sha256${salt.hex()}${digest.hex()}"


# ---------------------------------------------------------------------------
# 1. New bcrypt hashes: produce and verify
# ---------------------------------------------------------------------------

class TestBcryptHash:
    def test_produces_dollar_2b_prefix(self):
        h = get_password_hash("secret123")
        assert h.startswith("$2b$"), f"Expected $2b$ prefix, got: {h[:10]}"

    def test_correct_password_verifies(self):
        pw = "CorrectHorseBattery9"
        h = get_password_hash(pw)
        assert verify_password(pw, h) is True

    def test_wrong_password_rejected(self):
        pw = "CorrectHorseBattery9"
        h = get_password_hash(pw)
        assert verify_password("WrongPassword9", h) is False

    def test_cost_factor_is_12(self):
        h = get_password_hash("test_pw_99")
        # $2b$12$...
        parts = h.split("$")
        assert len(parts) >= 4
        assert int(parts[2]) == _BCRYPT_COST == 12

    def test_two_hashes_of_same_password_differ(self):
        """bcrypt uses a random salt — same password must not produce same hash."""
        pw = "SamePw123"
        assert get_password_hash(pw) != get_password_hash(pw)


# ---------------------------------------------------------------------------
# 2. CRITICAL ANTI-LOCKOUT: legacy pbkdf2 hashes still verify correctly
# ---------------------------------------------------------------------------

class TestLegacyPbkdf2Backward:
    def test_correct_password_on_legacy_hash_returns_true(self):
        pw = "MyOldPassword1"
        legacy_hash = _legacy_pbkdf2_hash(pw)
        assert legacy_hash.startswith("pbkdf2_sha256$")
        result = verify_password(pw, legacy_hash)
        assert result is True, (
            "LOCKOUT RISK: legacy pbkdf2 hash failed to verify — existing users would be locked out!"
        )

    def test_wrong_password_on_legacy_hash_returns_false(self):
        pw = "MyOldPassword1"
        legacy_hash = _legacy_pbkdf2_hash(pw)
        assert verify_password("WrongOldPassword1", legacy_hash) is False

    def test_legacy_hash_does_not_verify_against_bcrypt_path(self):
        """Ensure legacy hash string doesn't accidentally pass the bcrypt branch."""
        pw = "AnyPassword9"
        legacy_hash = _legacy_pbkdf2_hash(pw)
        # Must not start with $2 — confirm it takes the legacy branch
        assert not legacy_hash.startswith("$2")
        assert verify_password(pw, legacy_hash) is True

    def test_multiple_legacy_hashes(self):
        """Verify several different passwords against individually-generated legacy hashes."""
        passwords = ["alpha1", "Beta99!", "Γεια σου 1", "パスワード2"]
        for pw in passwords:
            legacy_hash = _legacy_pbkdf2_hash(pw)
            assert verify_password(pw, legacy_hash) is True, f"Failed for: {pw!r}"
            assert verify_password(pw + "X", legacy_hash) is False, f"Should have failed for: {pw!r}X"


# ---------------------------------------------------------------------------
# 3. password_needs_rehash
# ---------------------------------------------------------------------------

class TestPasswordNeedsRehash:
    def test_legacy_pbkdf2_needs_rehash(self):
        legacy_hash = _legacy_pbkdf2_hash("pw123")
        assert password_needs_rehash(legacy_hash) is True

    def test_fresh_bcrypt_does_not_need_rehash(self):
        h = get_password_hash("Fresh9Password")
        assert password_needs_rehash(h) is False

    def test_empty_string_needs_rehash(self):
        assert password_needs_rehash("") is True

    def test_garbage_needs_rehash(self):
        assert password_needs_rehash("not-a-real-hash") is True

    def test_bcrypt_with_wrong_cost_needs_rehash(self):
        """A $2b$ hash with a different cost factor should trigger rehash."""
        import bcrypt as _bcrypt
        prepared = _bcrypt_prepare("pw")
        # Produce a cost-10 hash
        low_cost = _bcrypt.hashpw(prepared, _bcrypt.gensalt(rounds=10)).decode()
        assert low_cost.startswith("$2b$10$")
        assert password_needs_rehash(low_cost) is True


# ---------------------------------------------------------------------------
# 4. Long / multibyte passwords — no silent truncation
# ---------------------------------------------------------------------------

class TestLongAndMultibytePasswords:
    def test_password_over_72_bytes_hashes_and_verifies(self):
        # 100 ASCII chars — well over bcrypt's 72-byte raw limit
        long_pw = "a" * 100
        h = get_password_hash(long_pw)
        assert verify_password(long_pw, h) is True
        # A truncated version (first 72 chars) must NOT verify
        truncated = "a" * 72
        assert verify_password(truncated, h) is False, (
            "TRUNCATION BUG: 72-char prefix of a 100-char password verified — "
            "sha256 pre-hashing is not working!"
        )

    def test_emoji_password_hashes_and_verifies(self):
        emoji_pw = "🐶🐱🐻🦊🐨🐼🐸🦁"  # 8 emoji, each 4 bytes = 32 bytes UTF-8
        h = get_password_hash(emoji_pw)
        assert verify_password(emoji_pw, h) is True
        assert verify_password("🐶🐱🐻🦊🐨🐼🐸🦋", h) is False  # last emoji differs

    def test_accented_characters_password(self):
        accented_pw = "Ãção_Pétala_Naïve_Über_99"
        h = get_password_hash(accented_pw)
        assert verify_password(accented_pw, h) is True
        assert verify_password("Acao_Petala_Naive_Uber_99", h) is False

    def test_very_long_unicode_password(self):
        # 200 accented chars → > 200 bytes UTF-8
        long_unicode = "ñ" * 200
        h = get_password_hash(long_unicode)
        assert verify_password(long_unicode, h) is True
        # Truncated at 72 raw bytes would be 36 'ñ' chars (each is 2 bytes)
        assert verify_password("ñ" * 36, h) is False, (
            "TRUNCATION BUG: 36-ñ prefix of a 200-ñ password verified!"
        )


# ---------------------------------------------------------------------------
# 5. Garbage / edge-case inputs — never raise
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_empty_hash_returns_false(self):
        assert verify_password("anypassword", "") is False

    def test_none_like_garbage_hash_returns_false(self):
        assert verify_password("pw", "garbage-that-is-not-a-real-hash") is False

    def test_partial_pbkdf2_prefix_returns_false(self):
        assert verify_password("pw", "pbkdf2_sha256$") is False

    def test_wrong_algorithm_in_pbkdf2_field_returns_false(self):
        assert verify_password("pw", "pbkdf2_sha512$aabbcc$ddeeff") is False

    def test_empty_password_with_real_hash(self):
        h = get_password_hash("realpassword9")
        assert verify_password("", h) is False

    def test_verify_never_raises_on_any_input(self):
        """verify_password must never raise regardless of inputs."""
        garbage_inputs = [
            ("pw", None),
            (None, "hash"),
            ("pw", 12345),
            ("pw", "$2b$12$"),          # truncated bcrypt hash
            ("pw", "$2b$12$" + "x" * 5),  # malformed bcrypt hash
        ]
        for plain, hashed in garbage_inputs:
            try:
                result = verify_password(plain, hashed)  # type: ignore[arg-type]
                assert result is False, f"Expected False for ({plain!r}, {hashed!r}), got {result}"
            except Exception as exc:
                pytest.fail(f"verify_password raised for ({plain!r}, {hashed!r}): {exc}")
