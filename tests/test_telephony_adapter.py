"""S1 — adaptador de telefonia: normalizador E.164 BR, NullProvider e registry.

Spec 2026-09-15 §1 (D4): ligação atrás de adaptador; TELEPHONY_PROVIDER=none
(ou ausente) → modo direto. Provedor mascarado só depois da escolha do Carlos.
"""
import logging

import pytest

from app.services.telephony import BridgeCallResult, NullProvider, get_telephony_provider, to_e164_br


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("(71) 98888-7777", "+5571988887777"),
        ("71 3599-9983", "+557135999983"),
        ("+55 71 98888-7777", "+5571988887777"),
        ("5571988887777", "+5571988887777"),
        ("0055 71 98888-7777", "+5571988887777"),
        ("071988887777", "+5571988887777"),
        ("55991234567", "+5555991234567"),
    ],
)
def test_to_e164_br_valid(raw, expected):
    assert to_e164_br(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "   ", "123", "0800 123 4567", "(71) 8888-7777", "11111111111"])
def test_to_e164_br_invalid_returns_none(raw):
    assert to_e164_br(raw) is None


def test_registry_defaults_to_null_provider(monkeypatch):
    monkeypatch.delenv("TELEPHONY_PROVIDER", raising=False)
    provider = get_telephony_provider()
    assert isinstance(provider, NullProvider)
    assert provider.name == "none"
    assert provider.is_enabled() is False


def test_registry_explicit_none(monkeypatch):
    monkeypatch.setenv("TELEPHONY_PROVIDER", " None ")
    assert isinstance(get_telephony_provider(), NullProvider)


def test_registry_unknown_provider_falls_back_to_null_with_warning(monkeypatch, caplog):
    monkeypatch.setenv("TELEPHONY_PROVIDER", "telnyx")
    with caplog.at_level(logging.WARNING, logger="aumigao.telephony"):
        provider = get_telephony_provider()
    assert isinstance(provider, NullProvider)
    assert "telnyx" in caplog.text


def test_null_provider_never_bridges():
    result = NullProvider().start_bridge_call("+5571999990000", "+5571988887777", None)
    assert result == BridgeCallResult(ok=False, provider_call_id=None, error="provider_disabled")
