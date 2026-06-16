"""B-ALT-011 — endurecimento do JWT (passo 1: claims de emissão).

O access token passa a carregar iat/iss/aud/jti além de sub/exp. jti dá um id único
por token (base para revogação futura); iss/aud identificam emissor/audiência; iat a
idade. RETROCOMPAT: a validação atual (get_current_user) decodifica só exp+assinatura,
então tokens novos continuam aceitos e tokens antigos (sem os claims) não quebram.
"""
import jwt

from app.core.security import (
    ALGORITHM,
    JWT_ISSUER,
    SECRET_KEY,
    create_access_token,
)


def _decode_strict(token: str) -> dict:
    # Passo 1: valida iss (não aud — aud entra no passo 2 com o enforcement).
    return jwt.decode(
        token,
        SECRET_KEY,
        algorithms=[ALGORITHM],
        issuer=JWT_ISSUER,
    )


def test_access_token_carries_hardening_claims():
    payload = _decode_strict(create_access_token("user-123"))
    assert payload["sub"] == "user-123"
    assert payload["iss"] == JWT_ISSUER
    assert isinstance(payload.get("jti"), str) and len(payload["jti"]) >= 16
    assert "iat" in payload
    assert "exp" in payload


def test_jti_is_unique_per_token():
    p1 = _decode_strict(create_access_token("u"))
    p2 = _decode_strict(create_access_token("u"))
    assert p1["jti"] != p2["jti"]


def test_extra_claims_preserved():
    payload = _decode_strict(create_access_token("u", {"role": "admin"}))
    assert payload["role"] == "admin"


def test_retrocompat_validation_without_audience_still_accepts():
    # get_current_user decodifica SEM exigir aud/iss; o token novo deve passar igual.
    token = create_access_token("user-xyz")
    payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    assert payload["sub"] == "user-xyz"
