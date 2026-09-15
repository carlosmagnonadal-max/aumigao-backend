"""Adaptador de telefonia do Botão de Emergência (S1)."""
from app.services.telephony.base import BridgeCallResult, TelephonyProvider
from app.services.telephony.null_provider import NullProvider
from app.services.telephony.phone import to_e164_br
from app.services.telephony.registry import get_telephony_provider
from app.services.telephony.twilio_provider import TelephonyProviderError, TwilioProvider

__all__ = [
    "BridgeCallResult",
    "NullProvider",
    "TelephonyProvider",
    "TelephonyProviderError",
    "TwilioProvider",
    "get_telephony_provider",
    "to_e164_br",
]
