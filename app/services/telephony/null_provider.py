"""NullProvider — telefonia desligada: o app liga direto pelo discador (modo direto)."""
from __future__ import annotations

from app.services.telephony.base import BridgeCallResult


class NullProvider:
    name = "none"

    def is_enabled(self) -> bool:
        return False

    def start_bridge_call(
        self, walker_e164: str, tutor_e164: str, caller_id: str | None
    ) -> BridgeCallResult:
        return BridgeCallResult(ok=False, provider_call_id=None, error="provider_disabled")
