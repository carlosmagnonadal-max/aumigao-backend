"""S1 — TwilioProvider: ligação ponte mascarada via Twilio Programmable Voice.

Sem chamada de rede real: toda a camada HTTP é substituída por
`httpx.MockTransport`. Cobre: request/auth/form corretos, TwiML escapado,
sucesso -> provider_call_id, erros HTTP/timeout/JSON inválido -> exceção sem
PII, seleção pelo registry (config completa x incompleta) e integração com
`walk_emergency_service` (modo masked / fallback para direct).
"""
from __future__ import annotations

import base64
import logging
import urllib.parse
import xml.etree.ElementTree as ET

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 — registra todas as tabelas no metadata
from app.core.database import Base
from app.models.pet import Pet
from app.models.tutor_profile import TutorProfile
from app.models.user import User
from app.models.walk import Walk
from app.models.walk_emergency_call import WalkEmergencyCall
from app.models.walker_profile import WalkerProfile
from app.services.telephony import BridgeCallResult, NullProvider, get_telephony_provider
from app.services.telephony.twilio_provider import (
    _SAY_TEXT_PT_BR,
    TelephonyProviderError,
    TwilioProvider,
    _build_twiml,
)
from app.services.walk_emergency_service import trigger_walk_emergency

ACCOUNT_SID = "ACfake0000000000000000000000000"
AUTH_TOKEN = "fake-secret-token-do-not-log"
CALLER_ID = "+15551234567"
WALKER_E164 = "+5571988880000"
TUTOR_E164 = "+5571999998888"


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


# ── start_bridge_call: request/auth/form/TwiML ───────────────────────────────

def test_start_bridge_call_posts_correct_request_and_returns_sid():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["headers"] = request.headers
        captured["body"] = request.content
        return httpx.Response(201, json={"sid": "CA123abc", "status": "queued"})

    provider = TwilioProvider(ACCOUNT_SID, AUTH_TOKEN, CALLER_ID, http_client=_client(handler))
    result = provider.start_bridge_call(WALKER_E164, TUTOR_E164, None)

    assert result == BridgeCallResult(ok=True, provider_call_id="CA123abc", error=None)
    assert captured["method"] == "POST"
    assert captured["url"] == f"https://api.twilio.com/2010-04-01/Accounts/{ACCOUNT_SID}/Calls.json"

    auth_header = captured["headers"]["authorization"]
    assert auth_header.startswith("Basic ")
    decoded = base64.b64decode(auth_header.split(" ", 1)[1]).decode()
    assert decoded == f"{ACCOUNT_SID}:{AUTH_TOKEN}"

    body = urllib.parse.parse_qs(captured["body"].decode())
    assert body["To"] == [WALKER_E164]
    assert body["From"] == [CALLER_ID]
    assert body["Timeout"] == ["30"]
    twiml = body["Twiml"][0]
    assert TUTOR_E164 in twiml
    assert f'callerId="{CALLER_ID}"' in twiml
    assert 'answerOnBridge="true"' in twiml
    assert 'language="pt-BR"' in twiml
    assert f"<Number>{TUTOR_E164}</Number>" in twiml


def test_start_bridge_call_uses_explicit_caller_id_override():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content
        return httpx.Response(201, json={"sid": "CA999"})

    provider = TwilioProvider(ACCOUNT_SID, AUTH_TOKEN, CALLER_ID, http_client=_client(handler))
    override_caller = "+15559998888"
    provider.start_bridge_call(WALKER_E164, TUTOR_E164, override_caller)

    body = urllib.parse.parse_qs(captured["body"].decode())
    assert body["From"] == [override_caller]
    assert f'callerId="{override_caller}"' in body["Twiml"][0]


def test_twiml_is_valid_xml_and_escapes_special_chars():
    tutor = '+15551234567"><Hangup/>'
    caller = '+1555&"<evil>'
    twiml = _build_twiml(tutor, caller)

    # Se o escaping estiver errado o XML fica malformado e isto levanta ParseError.
    root = ET.fromstring(twiml)
    dial = root.find("Dial")
    assert dial.get("callerId") == caller
    assert dial.find("Number").text == tutor
    say = root.find("Say")
    assert say.get("language") == "pt-BR"
    assert say.text == _SAY_TEXT_PT_BR


# ── Erros: HTTP/timeout/JSON inválido, sem PII na mensagem ──────────────────

def test_http_4xx_raises_telephony_provider_error_without_pii():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"message": f"Invalid To number {WALKER_E164}", "code": 21211})

    provider = TwilioProvider(ACCOUNT_SID, AUTH_TOKEN, CALLER_ID, http_client=_client(handler))
    with pytest.raises(TelephonyProviderError) as exc_info:
        provider.start_bridge_call(WALKER_E164, TUTOR_E164, None)

    message = str(exc_info.value)
    assert WALKER_E164 not in message
    assert TUTOR_E164 not in message
    assert AUTH_TOKEN not in message


def test_http_5xx_raises_telephony_provider_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal error")

    provider = TwilioProvider(ACCOUNT_SID, AUTH_TOKEN, CALLER_ID, http_client=_client(handler))
    with pytest.raises(TelephonyProviderError):
        provider.start_bridge_call(WALKER_E164, TUTOR_E164, None)


def test_timeout_raises_telephony_provider_error_without_pii():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    provider = TwilioProvider(ACCOUNT_SID, AUTH_TOKEN, CALLER_ID, http_client=_client(handler))
    with pytest.raises(TelephonyProviderError) as exc_info:
        provider.start_bridge_call(WALKER_E164, TUTOR_E164, None)
    message = str(exc_info.value)
    assert WALKER_E164 not in message
    assert AUTH_TOKEN not in message


def test_invalid_json_response_raises_telephony_provider_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, text="not-json-at-all")

    provider = TwilioProvider(ACCOUNT_SID, AUTH_TOKEN, CALLER_ID, http_client=_client(handler))
    with pytest.raises(TelephonyProviderError):
        provider.start_bridge_call(WALKER_E164, TUTOR_E164, None)


def test_missing_sid_raises_telephony_provider_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={"status": "queued"})

    provider = TwilioProvider(ACCOUNT_SID, AUTH_TOKEN, CALLER_ID, http_client=_client(handler))
    with pytest.raises(TelephonyProviderError):
        provider.start_bridge_call(WALKER_E164, TUTOR_E164, None)


# ── Registry: seleção do TwilioProvider por env ──────────────────────────────

def test_registry_selects_twilio_with_full_config(monkeypatch):
    monkeypatch.setenv("TELEPHONY_PROVIDER", "twilio")
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", ACCOUNT_SID)
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", AUTH_TOKEN)
    monkeypatch.setenv("TELEPHONY_CALLER_ID", CALLER_ID)

    provider = get_telephony_provider()

    assert isinstance(provider, TwilioProvider)
    assert provider.name == "twilio"
    assert provider.is_enabled() is True


@pytest.mark.parametrize("missing_env", ["TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TELEPHONY_CALLER_ID"])
def test_registry_twilio_incomplete_config_falls_back_to_null(monkeypatch, caplog, missing_env):
    monkeypatch.setenv("TELEPHONY_PROVIDER", "twilio")
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", ACCOUNT_SID)
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", AUTH_TOKEN)
    monkeypatch.setenv("TELEPHONY_CALLER_ID", CALLER_ID)
    monkeypatch.delenv(missing_env, raising=False)

    with caplog.at_level(logging.WARNING, logger="aumigao.telephony"):
        provider = get_telephony_provider()

    assert isinstance(provider, NullProvider)
    assert missing_env in caplog.text
    assert AUTH_TOKEN not in caplog.text


def test_registry_twilio_invalid_caller_id_falls_back_to_null(monkeypatch, caplog):
    monkeypatch.setenv("TELEPHONY_PROVIDER", "twilio")
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", ACCOUNT_SID)
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", AUTH_TOKEN)
    monkeypatch.setenv("TELEPHONY_CALLER_ID", "0800-not-e164")

    with caplog.at_level(logging.WARNING, logger="aumigao.telephony"):
        provider = get_telephony_provider()

    assert isinstance(provider, NullProvider)
    assert "CALLER_ID" in caplog.text


# ── Integração com walk_emergency_service ────────────────────────────────────

WALKER_ID = "walker-twilio-1"
TUTOR_ID = "tutor-twilio-1"
PET_ID = "pet-twilio-1"
WALK_ID = "walk-twilio-1"
WALKER_PHONE_E164 = "+5571999990000"
TUTOR_PHONE_E164 = "+5571988887777"


@pytest.fixture()
def db(monkeypatch):
    monkeypatch.delenv("TELEPHONY_PROVIDER", raising=False)
    monkeypatch.setattr(
        "app.routes.notifications.send_push_for_notification_background",
        lambda *args, **kwargs: None,
    )
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(User(id=WALKER_ID, email="walker-twilio@test.com", password_hash="x", role="walker", full_name="Walker"))
    session.add(User(id=TUTOR_ID, email="tutor-twilio@test.com", password_hash="x", role="tutor", full_name="Tutor"))
    session.add(WalkerProfile(
        id="wp-twilio-1", user_id=WALKER_ID, status="active", active_as_walker=True,
        full_name="Walker", phone="(71) 99999-0000",
    ))
    session.add(TutorProfile(id="tp-twilio-1", user_id=TUTOR_ID, phone="(71) 98888-7777"))
    session.add(Pet(id=PET_ID, name="Rex", species="Cachorro", tutor_id=TUTOR_ID))
    session.add(Walk(
        id=WALK_ID, tutor_id=TUTOR_ID, walker_id=WALKER_ID, pet_id=PET_ID,
        scheduled_date="2026-09-15T10:00:00", duration_minutes=30, price=50.0,
        status="Passeando agora", operational_status="ride_in_progress",
    ))
    session.commit()
    yield session
    session.close()


def _walk_and_user(db):
    walk = db.get(Walk, WALK_ID)
    user = db.get(User, WALKER_ID)
    return walk, user


def test_walk_emergency_with_twilio_mocked_returns_masked_mode_without_tutor_phone(db, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={"sid": "CAbridge123"})

    provider = TwilioProvider(ACCOUNT_SID, AUTH_TOKEN, CALLER_ID, http_client=_client(handler))
    monkeypatch.setattr("app.services.walk_emergency_service.get_telephony_provider", lambda: provider)

    walk, user = _walk_and_user(db)
    body = trigger_walk_emergency(db, walk, user, "pet_mal")

    assert body["mode"] == "masked"
    assert body["status"] == "initiated"
    assert body["tutor_phone_e164"] is None  # nunca vaza em modo mascarado
    call = db.get(WalkEmergencyCall, body["call_id"])
    assert call.provider == "twilio"
    assert call.provider_call_id == "CAbridge123"
    assert call.status == "initiated"


def test_walk_emergency_with_twilio_failing_falls_back_to_direct(db, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="twilio fora do ar")

    provider = TwilioProvider(ACCOUNT_SID, AUTH_TOKEN, CALLER_ID, http_client=_client(handler))
    monkeypatch.setattr("app.services.walk_emergency_service.get_telephony_provider", lambda: provider)

    walk, user = _walk_and_user(db)
    body = trigger_walk_emergency(db, walk, user, "pet_mal")

    assert body["mode"] == "direct"
    assert body["status"] == "fallback_direct"
    assert body["tutor_phone_e164"] == TUTOR_PHONE_E164
    call = db.get(WalkEmergencyCall, body["call_id"])
    assert call.fallback_reason == "provider_exception"
