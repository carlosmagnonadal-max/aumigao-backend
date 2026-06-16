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
    # B-ALT-011: além de sub/exp, o token carrega iat (idade), iss/aud (emissor/
    # audiência) e jti (id único — base para revogação). A validação atual checa só
    # exp+assinatura, então isto é retrocompatível (tokens antigos seguem válidos).
    payload: dict[str, Any] = {
        "sub": subject,
        "iat": now,
        "exp": expire,
        "iss": JWT_ISSUER,
        # "aud" NÃO é emitido aqui de propósito: o get_current_user atual decodifica
        # sem passar audience, e o PyJWT exige audience quando o token traz aud — emitir
        # aud agora quebraria a validação. aud entra no passo 2, junto do enforcement.
        "jti": uuid.uuid4().hex,
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)
