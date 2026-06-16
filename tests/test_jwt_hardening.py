"""B-ALT-011 — endurecimento do JWT (claims de emissão).

O access token carrega iat/iss/aud/jti além de sub/exp. jti dá um id único por token
(base para revogação futura); iss/aud identificam emissor/audiência; iat a idade.

NOTA: a partir do passo 2a o token TAMBÉM emite aud e o decode canônico passa a ser o
decode_access_token (valida assinatura+exp+iss+aud, com retrocompat p/ tokens legados).
Os testes do enforcement/retrocompat ficam em test_jwt_enforcement.py.
"""
from app.core.security import (
    JWT_AUDIENCE,
    JWT_ISSUER,
    create_access_token,
    decode_access_token,
)


def test_access_token_carries_hardening_claims():
    payload = decode_access_token(create_access_token("user-123"))
    assert payload["sub"] == "user-123"
    assert payload["iss"] == JWT_ISSUER
    assert payload["aud"] == JWT_AUDIENCE
    assert isinstance(payload.get("jti"), str) and len(payload["jti"]) >= 16
    assert "iat" in payload
    assert "exp" in payload


def test_jti_is_unique_per_token():
    p1 = decode_access_token(create_access_token("u"))
    p2 = decode_access_token(create_access_token("u"))
    assert p1["jti"] != p2["jti"]


def test_extra_claims_preserved():
    payload = decode_access_token(create_access_token("u", {"role": "admin"}))
    assert payload["role"] == "admin"
