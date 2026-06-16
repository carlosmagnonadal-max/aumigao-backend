"""B-ALT-011 — endurecimento do JWT (passo 2a: emissao de aud + enforcement iss/aud).

O passo 1 passou a emitir iat/iss/jti (mas NAO aud, porque o decode antigo nao passava
audience e o PyJWT exige audience quando o token traz aud). O passo 2a:
  1. passa a EMITIR aud no token;
  2. introduz decode_access_token(), que valida assinatura+exp+iss+aud.

ENFORCEMENT RETROCOMPATIVEL: tokens LEGADOS (emitidos antes deste passo, sem iss/aud)
sao ACEITOS durante a janela de expiracao (TTL) — senao todo usuario logado seria
deslogado no deploy. Mas um token que TRAZ iss/aud ERRADOS e REJEITADO (protege contra
reuso entre servicos). Quando os tokens legados expirarem, o fallback pode sair.
"""
from datetime import datetime, timedelta, timezone

import jwt
import pytest

from app.core.security import (
    ALGORITHM,
    JWT_AUDIENCE,
    JWT_ISSUER,
    SECRET_KEY,
    create_access_token,
    decode_access_token,
)


def _encode(claims: dict, key: str = SECRET_KEY) -> str:
    base = {"sub": "user-1", "exp": datetime.now(timezone.utc) + timedelta(hours=1)}
    base.update(claims)
    return jwt.encode(base, key, algorithm=ALGORITHM)


# --- emissao -------------------------------------------------------------------

def test_create_access_token_now_emits_aud():
    token = create_access_token("user-123")
    # Lê sem checar aud para inspecionar o claim cru.
    payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM], options={"verify_aud": False})
    assert payload["aud"] == JWT_AUDIENCE
    assert payload["iss"] == JWT_ISSUER


# --- aceitacao -----------------------------------------------------------------

def test_decode_accepts_freshly_minted_token():
    payload = decode_access_token(create_access_token("user-xyz"))
    assert payload["sub"] == "user-xyz"
    assert payload["aud"] == JWT_AUDIENCE
    assert payload["iss"] == JWT_ISSUER


def test_decode_accepts_legacy_token_without_aud_or_iss():
    # Token legado (so sub/exp) — retrocompat: deve passar durante a transicao.
    legacy = _encode({})  # sem aud, sem iss
    payload = decode_access_token(legacy)
    assert payload["sub"] == "user-1"


# --- rejeicao ------------------------------------------------------------------

def test_decode_rejects_wrong_audience_even_without_iss():
    # Caso critico de ordem: aud ERRADO e iss AUSENTE nao pode cair no fallback de legado.
    bad = _encode({"aud": "outro-servico"})
    with pytest.raises(jwt.InvalidAudienceError):
        decode_access_token(bad)


def test_decode_rejects_wrong_issuer():
    bad = _encode({"aud": JWT_AUDIENCE, "iss": "emissor-malicioso"})
    with pytest.raises(jwt.InvalidIssuerError):
        decode_access_token(bad)


def test_decode_rejects_expired_token():
    expired = jwt.encode(
        {"sub": "u", "aud": JWT_AUDIENCE, "iss": JWT_ISSUER,
         "exp": datetime.now(timezone.utc) - timedelta(seconds=1)},
        SECRET_KEY, algorithm=ALGORITHM,
    )
    with pytest.raises(jwt.ExpiredSignatureError):
        decode_access_token(expired)


def test_decode_rejects_bad_signature():
    forged = _encode({"aud": JWT_AUDIENCE, "iss": JWT_ISSUER}, key="x" * 40)
    with pytest.raises(jwt.InvalidSignatureError):
        decode_access_token(forged)
