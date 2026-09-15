"""Contrato do adaptador de telefonia (S1 — Botão de Emergência, D4)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class BridgeCallResult:
    """Resultado de uma tentativa de ponte mascarada passeador ↔ tutor."""

    ok: bool
    provider_call_id: str | None = None
    error: str | None = None


class TelephonyProvider(Protocol):
    """Provedor de ligação mascarada. Sem gravação de áudio (D4)."""

    name: str

    def is_enabled(self) -> bool: ...

    def start_bridge_call(
        self, walker_e164: str, tutor_e164: str, caller_id: str | None
    ) -> BridgeCallResult: ...
