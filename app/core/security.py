from datetime import datetime, timedelta, timezone
from hashlib import pbkdf2_hmac
from hmac import compare_digest
import os
import uuid
from os import urandom
from pathlib import Path
from typing import Any

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
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7
# B-ALT-011: emissor/audiência do token (configuráveis por env). Identificam de quem é
# o token e para qual app, e preparam o enforcement (passo 2) e a revogação (via jti).
JWT_ISSUER = (os.getenv("JWT_ISSUER") or "aumigao-walk").strip()
JWT_AUDIENCE = (os.getenv("JWT_AUDIENCE") or "aumigao-app").strip()


def get_password_hash(password: str) -> str:
    salt = urandom(16)
    digest = pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 120_000)
    return f"pbkdf2_sha256${salt.hex()}${digest.hex()}"


def verify_password(plain_password: str, hashed_password: str) -> bool:
    try:
        algorithm, salt_hex, digest_hex = hashed_password.split("$", 2)
        if algorithm != "pbkdf2_sha256":
            return False
        digest = pbkdf2_hmac("sha256", plain_password.encode("utf-8"), bytes.fromhex(salt_hex), 120_000)
        return compare_digest(digest.hex(), digest_hex)
    except Exception:
        return False


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
    aud = payload.get("aud")
    if aud is not None and aud != JWT_AUDIENCE:
        raise jwt.InvalidAudienceError("Audiencia do token invalida")
    iss = payload.get("iss")
    if iss is not None and iss != JWT_ISSUER:
        raise jwt.InvalidIssuerError("Emissor do token invalido")
    return payload
