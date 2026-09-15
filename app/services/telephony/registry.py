"""Seleção do provedor de telefonia por env (TELEPHONY_PROVIDER).

none/ausente → NullProvider (modo direto, ativo hoje). Os provedores mascarados
(Telnyx × NVoIP × Twilio) entram em IMPLEMENTED_PROVIDERS só depois da escolha do
Carlos. Um valor desconhecido NUNCA quebra a emergência: cai no modo direto com log.

TELEPHONY_PROVIDER=twilio exige TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN e
TELEPHONY_CALLER_ID (E.164 válido, ex. número Twilio "+1..."); faltando ou
inválida qualquer uma, cai no modo direto com aviso (sem vazar segredo/telefone).
"""
from __future__ import annotations

import logging
import os
import re

from app.services.telephony.base import TelephonyProvider
from app.services.telephony.null_provider import NullProvider

LOGGER = logging.getLogger("aumigao.telephony")

_NULL_PROVIDER = NullProvider()
IMPLEMENTED_PROVIDERS: dict[str, TelephonyProvider] = {}

# E.164 genérico (não só BR): "+" seguido de 8 a 15 dígitos, sem zero à esquerda
# no código do país — cobre o número Twilio usado como TELEPHONY_CALLER_ID
# (tipicamente "+1XXXXXXXXXX" nos EUA, mas pode ser BR ou outro país).
_E164_RE = re.compile(r"^\+[1-9]\d{7,14}$")


def _build_twilio_provider() -> TelephonyProvider | None:
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    caller_id = os.getenv("TELEPHONY_CALLER_ID")

    missing = [
        env_name
        for env_name, value in (
            ("TWILIO_ACCOUNT_SID", account_sid),
            ("TWILIO_AUTH_TOKEN", auth_token),
            ("TELEPHONY_CALLER_ID", caller_id),
        )
        if not (value or "").strip()
    ]
    if missing:
        LOGGER.warning(
            "TELEPHONY_PROVIDER=twilio sem config completa (faltam: %s); "
            "usando modo direto (NullProvider).",
            ", ".join(missing),
        )
        return None

    caller_id = caller_id.strip()
    if not _E164_RE.match(caller_id):
        LOGGER.warning(
            "TELEPHONY_PROVIDER=twilio com TELEPHONY_CALLER_ID fora do formato E.164; "
            "usando modo direto (NullProvider).",
        )
        return None

    from app.services.telephony.twilio_provider import TwilioProvider

    return TwilioProvider(account_sid.strip(), auth_token.strip(), caller_id)


def get_telephony_provider() -> TelephonyProvider:
    key = (os.getenv("TELEPHONY_PROVIDER") or "none").strip().lower()
    if key in ("", "none"):
        return _NULL_PROVIDER
    if key == "twilio":
        provider = _build_twilio_provider()
        return provider if provider is not None else _NULL_PROVIDER
    provider = IMPLEMENTED_PROVIDERS.get(key)
    if provider is None:
        LOGGER.warning(
            "TELEPHONY_PROVIDER=%s sem implementacao; usando modo direto (NullProvider).", key
        )
        return _NULL_PROVIDER
    return provider
