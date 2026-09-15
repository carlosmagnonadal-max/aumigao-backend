"""Seleção do provedor de telefonia por env (TELEPHONY_PROVIDER).

none/ausente → NullProvider (modo direto, ativo hoje). Os provedores mascarados
(Telnyx × NVoIP × Twilio) entram em IMPLEMENTED_PROVIDERS só depois da escolha do
Carlos. Um valor desconhecido NUNCA quebra a emergência: cai no modo direto com log.
"""
from __future__ import annotations

import logging
import os

from app.services.telephony.base import TelephonyProvider
from app.services.telephony.null_provider import NullProvider

LOGGER = logging.getLogger("aumigao.telephony")

_NULL_PROVIDER = NullProvider()
IMPLEMENTED_PROVIDERS: dict[str, TelephonyProvider] = {}


def get_telephony_provider() -> TelephonyProvider:
    key = (os.getenv("TELEPHONY_PROVIDER") or "none").strip().lower()
    if key in ("", "none"):
        return _NULL_PROVIDER
    provider = IMPLEMENTED_PROVIDERS.get(key)
    if provider is None:
        LOGGER.warning(
            "TELEPHONY_PROVIDER=%s sem implementacao; usando modo direto (NullProvider).", key
        )
        return _NULL_PROVIDER
    return provider
