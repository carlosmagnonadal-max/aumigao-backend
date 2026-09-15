"""Adaptador de telefonia do Botão de Emergência (S1)."""
from app.services.telephony.base import BridgeCallResult, TelephonyProvider
from app.services.telephony.null_provider import NullProvider
from app.services.telephony.phone import to_e164_br
from app.services.telephony.registry import get_telephony_provider

__all__ = [
    "BridgeCallResult",
    "NullProvider",
    "TelephonyProvider",
    "get_telephony_provider",
    "to_e164_br",
]
