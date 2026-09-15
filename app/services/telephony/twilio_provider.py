"""TwilioProvider — ligação ponte mascarada via Twilio Programmable Voice (S1).

A Twilio liga primeiro para o PASSEADOR; quando ele atende, o TwiML inline
(sem webhook/status callback) conecta a chamada ao TUTOR com `callerId`
mascarado — nenhum dos dois vê o número real do outro (D4 do plano).

Envs exigidas para este provedor entrar em vigor (lidas pelo registry,
TELEPHONY_PROVIDER=twilio):
  TWILIO_ACCOUNT_SID   — SID da conta Twilio (Basic Auth, usuário).
  TWILIO_AUTH_TOKEN    — token da conta Twilio (Basic Auth, senha) — NUNCA logar.
  TELEPHONY_CALLER_ID  — número Twilio em E.164 usado como `From`/`callerId`.
Faltando qualquer uma, o registry cai no modo direto (NullProvider) — ver
app/services/telephony/registry.py.

Sem SDK da Twilio: só `httpx` (já é dependência do projeto) fazendo o POST
REST puro em /Calls.json.
"""
from __future__ import annotations

import logging
from xml.sax.saxutils import escape, quoteattr

import httpx

from app.services.telephony.base import BridgeCallResult

LOGGER = logging.getLogger("aumigao.telephony")

TWILIO_API_BASE = "https://api.twilio.com/2010-04-01"
_BRIDGE_TIMEOUT_SECONDS = "30"
_SAY_TEXT_PT_BR = "Emergência no passeio. Conectando você ao tutor."


class TelephonyProviderError(Exception):
    """Erro do provedor de telefonia. A mensagem NUNCA carrega telefone/token."""


def _build_twiml(tutor_e164: str, caller_id: str) -> str:
    """TwiML inline enviado no próprio POST — sem endpoint de webhook."""
    say = escape(_SAY_TEXT_PT_BR)
    caller_attr = quoteattr(caller_id)
    number = escape(tutor_e164)
    return (
        "<Response>"
        f'<Say language="pt-BR">{say}</Say>'
        f'<Dial callerId={caller_attr} timeout="30" answerOnBridge="true">'
        f"<Number>{number}</Number>"
        "</Dial>"
        "</Response>"
    )


class TwilioProvider:
    """Implementa TelephonyProvider (app/services/telephony/base.py) via REST da Twilio."""

    name = "twilio"

    def __init__(
        self,
        account_sid: str,
        auth_token: str,
        caller_id: str,
        *,
        http_client: httpx.Client | None = None,
        timeout: float = 5.0,
    ) -> None:
        self._account_sid = account_sid
        self._auth_token = auth_token
        self._caller_id = caller_id
        self._http_client = http_client
        self._timeout = timeout

    def is_enabled(self) -> bool:
        return True

    def start_bridge_call(
        self, walker_e164: str, tutor_e164: str, caller_id: str | None
    ) -> BridgeCallResult:
        effective_caller_id = caller_id or self._caller_id
        twiml = _build_twiml(tutor_e164, effective_caller_id)
        url = f"{TWILIO_API_BASE}/Accounts/{self._account_sid}/Calls.json"
        form_data = {
            "To": walker_e164,
            "From": effective_caller_id,
            "Twiml": twiml,
            "Timeout": _BRIDGE_TIMEOUT_SECONDS,
        }

        client = self._http_client
        owns_client = client is None
        if owns_client:
            client = httpx.Client(timeout=self._timeout)
        try:
            try:
                response = client.post(
                    url,
                    data=form_data,
                    auth=(self._account_sid, self._auth_token),
                )
            except httpx.TimeoutException as exc:
                raise TelephonyProviderError("twilio_timeout") from exc
            except httpx.HTTPError as exc:
                raise TelephonyProviderError("twilio_request_failed") from exc
        finally:
            if owns_client:
                client.close()

        if response.status_code >= 400:
            LOGGER.error(
                "twilio_call_http_error",
                extra={"status_code": response.status_code},
            )
            raise TelephonyProviderError(f"twilio_http_{response.status_code}")

        try:
            payload = response.json()
        except ValueError as exc:
            raise TelephonyProviderError("twilio_invalid_json_response") from exc

        sid = payload.get("sid") if isinstance(payload, dict) else None
        if not sid:
            raise TelephonyProviderError("twilio_missing_call_sid")

        return BridgeCallResult(ok=True, provider_call_id=sid, error=None)
