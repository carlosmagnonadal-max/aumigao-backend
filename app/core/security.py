import base64
import hashlib
from datetime import datetime, timedelta, timezone
from hashlib import pbkdf2_hmac
from hmac import compare_digest
import os
import uuid
from os import urandom
from pathlib import Path
from typing import Any

import bcrypt
import jwt
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

SECRET_KEY = (os.getenv("JWT_SECRET") or "").strip().strip('"').strip("'")

if not SECRET_KEY or len(SECRET_KEY) < 32:
    raise RuntimeError(
        "JWT_SECRET ausente ou curto demais. "
        "Defina uma variável de ambiente com pelo menos 32 bytes."
    )

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES") or str(60 * 4))  # 4h default (EPIC 4.1: 7d→24h→4h; env-overridable). Janela menor p/ token roubado; refresh silencioso no app cobre a UX.
REFRESH_TOKEN_EXPIRE_DAYS = int(os.getenv("REFRESH_TOKEN_EXPIRE_DAYS") or "30")
# B-ALT-011: emissor/audiência do token (configuráveis por env). Identificam de quem é
# o token e para qual app, e preparam o enforcement (passo 2) e a revogação (via jti).
JWT_ISSUER = (os.getenv("JWT_ISSUER") or "aumigao-walk").strip()
JWT_AUDIENCE = (os.getenv("JWT_AUDIENCE") or "aumigao-app").strip()


_BCRYPT_COST = 12


def _bcrypt_prepare(password: str) -> bytes:
    """Pre-hash the password with SHA-256 then base64-encode before bcrypt.

    bcrypt silently truncates inputs at 72 bytes; this step ensures that long
    or multibyte passwords are never truncated — every bit of the original
    password influences the final hash.
    """
    digest = hashlib.sha256(password.encode("utf-8")).digest()
    return base64.b64encode(digest)


def get_password_hash(password: str) -> str:
    """Hash password with bcrypt (cost factor 12). Returns a $2b$… string."""
    prepared = _bcrypt_prepare(password)
    hashed = bcrypt.hashpw(prepared, bcrypt.gensalt(rounds=_BCRYPT_COST))
    return hashed.decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify password against hash — backward-compatible with all hash formats.

    Dispatch:
    - $2… prefix          → bcrypt path (new hashes produced by get_password_hash).
    - bcrypt_pbkdf2$…     → layered path: pbkdf2 inner digest verified via bcrypt
                            (DB-migrated users; SQL used pgcrypto crypt/gen_salt).
    - pbkdf2_sha256$…     → legacy pbkdf2 path (pre-migration hashes).
    - anything else       → False.
    Never raises; all exceptions are caught and return False.
    """
    try:
        if hashed_password.startswith("$2"):
            # bcrypt path — use try/except BaseException because bcrypt's Rust
            # backend can raise PanicException (not a subclass of Exception) on
            # malformed/truncated hash strings.
            try:
                prepared = _bcrypt_prepare(plain_password)
                return bcrypt.checkpw(prepared, hashed_password.encode("utf-8"))
            except BaseException:
                return False
        elif hashed_password.startswith("bcrypt_pbkdf2$"):
            # Layered hash: bcrypt_pbkdf2$<salt_hex>$<bcrypt_of_digest_hex>
            # Produced by the SQL migration (pgcrypto crypt/gen_salt bf,12) or by
            # wrap_pbkdf2_with_bcrypt(). The bcrypt_part starts with $2a$ (pgcrypto)
            # or $2b$ (Python bcrypt) and may contain $, so split at most 2 times.
            try:
                _prefix, salt_hex, bcrypt_part = hashed_password.split("$", 2)
                inner_hex = pbkdf2_hmac(
                    "sha256",
                    plain_password.encode("utf-8"),
                    bytes.fromhex(salt_hex),
                    120_000,
                ).hex()  # 64-char lowercase hex string — under bcrypt's 72-byte limit
                return bcrypt.checkpw(inner_hex.encode("utf-8"), bcrypt_part.encode("utf-8"))
            except BaseException:
                return False
        elif hashed_password.startswith("pbkdf2_sha256$"):
            # Legacy pbkdf2 path — keep existing users working
            algorithm, salt_hex, digest_hex = hashed_password.split("$", 2)
            if algorithm != "pbkdf2_sha256":
                return False
            digest = pbkdf2_hmac(
                "sha256",
                plain_password.encode("utf-8"),
                bytes.fromhex(salt_hex),
                120_000,
            )
            return compare_digest(digest.hex(), digest_hex)
        else:
            return False
    except Exception:
        return False


def wrap_pbkdf2_with_bcrypt(pbkdf2_hash: str) -> str:
    """Re-wrap a legacy pbkdf2_sha256 hash into the layered bcrypt_pbkdf2 format.

    Mirrors what the SQL migration does via pgcrypto:
      crypt(<digest_hex>, gen_salt('bf', 12))
    but executed in Python. The digest_hex is 64 ASCII chars (SHA-256 hex),
    which is 64 bytes — safely under bcrypt's 72-byte truncation limit.

    Args:
        pbkdf2_hash: a string of the form ``pbkdf2_sha256$<salt_hex>$<digest_hex>``.

    Returns:
        ``bcrypt_pbkdf2$<salt_hex>$<bcrypt_of_digest_hex>``

    Raises:
        ValueError: if pbkdf2_hash is not in the expected format.
    """
    if not pbkdf2_hash.startswith("pbkdf2_sha256$"):
        raise ValueError(f"Expected pbkdf2_sha256$ prefix, got: {pbkdf2_hash[:20]!r}")
    _algorithm, salt_hex, digest_hex = pbkdf2_hash.split("$", 2)
    bcrypt_hash = bcrypt.hashpw(digest_hex.encode("utf-8"), bcrypt.gensalt(rounds=12))
    return f"bcrypt_pbkdf2${salt_hex}${bcrypt_hash.decode('utf-8')}"


def password_needs_rehash(hashed: str) -> bool:
    """Return True if the hash is NOT a current bcrypt hash at cost 12.

    Used in the login flow to transparently migrate legacy pbkdf2 users to
    bcrypt on their next successful login.
    """
    if not hashed.startswith("$2b$"):
        return True
    try:
        # bcrypt hash format: $2b$NN$<53 chars>
        # Split on '$' → ['', '2b', 'NN', '<salt+hash>']
        parts = hashed.split("$")
        if len(parts) < 4:
            return True
        cost = int(parts[2])
        return cost != _BCRYPT_COST
    except Exception:
        return True


def create_refresh_token(user: Any) -> str:
    """Emite um refresh token de longa duração (REFRESH_TOKEN_EXPIRE_DAYS dias).

    Claims:
    - type: "refresh"  — identifica o propósito; decode_access_token rejeita este tipo.
    - sub: user.id
    - ver: user.token_version  — permite revogar ao trocar/redefinir a senha.
    - jti: UUID único  — base para revogação por token no futuro.
    - exp: agora + REFRESH_TOKEN_EXPIRE_DAYS dias.

    Usa o mesmo JWT_SECRET / HS256 do access token — o claim `type` distingue o uso.
    """
    now = datetime.now(timezone.utc)
    expire = now + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
    payload: dict[str, Any] = {
        "sub": user.id,
        "type": "refresh",
        "ver": user.token_version or 0,
        "jti": uuid.uuid4().hex,
        "iat": now,
        "exp": expire,
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def create_access_token(subject: str, extra: dict[str, Any] | None = None) -> str:
    now = datetime.now(timezone.utc)
    expire = now + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    # B-ALT-011 (passo 2a): além de sub/exp, o token carrega iat (idade), iss/aud
    # (emissor/audiência) e jti (id único — base para revogação). O decode passa a ser
    # feito por decode_access_token, que valida iss/aud de forma retrocompatível.
    payload: dict[str, Any] = {
        "sub": subject,
        "iat": now,
        "exp": expire,
        "iss": JWT_ISSUER,
        "aud": JWT_AUDIENCE,
        "jti": uuid.uuid4().hex,
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def decode_access_token(token: str) -> dict[str, Any]:
    """Decodifica e valida o access token (assinatura, exp, iss e aud).

    B-ALT-011 (passo 2a) — enforcement RETROCOMPATÍVEL. Decodificamos primeiro só
    assinatura+exp (verify_aud desligado, independente de o token trazer aud ou não) e
    enforçamos iss/aud manualmente com a regra de transição:
      - claim PRESENTE precisa bater (senão rejeita — protege contra reuso entre serviços);
      - claim AUSENTE é aceito (token legado emitido antes do enforcement — não desloga
        usuários durante a janela de expiração/TTL).
    Fazer a checagem manual (em vez de passar audience/issuer ao jwt.decode) torna o
    enforcement independente da ordem em que o PyJWT validaria os claims — um token com
    aud errado e iss ausente NÃO pode escapar como se fosse legado.
    Quando os tokens legados expirarem, dá para exigir os claims (remover o fallback).
    """
    payload = jwt.decode(
        token,
        SECRET_KEY,
        algorithms=[ALGORITHM],
        options={"verify_aud": False},
    )
    # sec/jwt-refresh: rejeita refresh tokens explicitamente.
    # RETROCOMPAT: tokens legados/access NÃO têm claim `type` → aceitos normalmente.
    # Só rejeita se `type == "refresh"` for explícito — nunca false-positive em tokens legados.
    if payload.get("type") == "refresh":
        raise jwt.InvalidTokenError("Refresh token nao pode ser usado como access token")
    aud = payload.get("aud")
    if aud is not None and aud != JWT_AUDIENCE:
        raise jwt.InvalidAudienceError("Audiencia do token invalida")
    iss = payload.get("iss")
    if iss is not None and iss != JWT_ISSUER:
        raise jwt.InvalidIssuerError("Emissor do token invalido")
    return payload
