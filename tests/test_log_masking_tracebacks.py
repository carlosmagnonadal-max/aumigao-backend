"""Sec-audit 2026-08-04 (médio, comprovado em prod): masking de logs pulava exc_info.

Um traceback do httpx (LocalProtocolError "Illegal header value b'Bearer re_...\\r\\n'")
vazou a chave Resend inteira no Cloud Logging. Estes testes garantem que tracebacks
(exc_info/exc_text), stack_info, cadeias de exceção e exceções passadas como args
saem mascarados — nos formatters JSON e texto — sem quebrar o JSON.
"""
from __future__ import annotations

import io
import json
import logging

import pytest

from app.core import logging_config
from app.core.log_masking import MaskingFormatterMixin, SensitiveDataFilter, _mask_string

RESEND_KEY = "re_Ab12Cd34Ef56Gh78Ij90KlMn"
BEARER_TOKEN = "sk-live-9f8e7d6c5b4a3210zz"
PASSWORD = "Hunter2Secreta!"
CPF = "123.456.789-09"
EMAIL = "carlos.titular@example.com"
JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyLTEyMyJ9.c2lnbmF0dXJlLXZhbHVl"
ASAAS_KEY = "$aact_prod_000MzkwODA2MWY2OGM3MWRlMDU2NWM3MzJlNzZmNGZhZGY6OjAwMDA"
DB_PASS = "pgS3nhaForte"

RAW_SECRETS = [RESEND_KEY, BEARER_TOKEN, PASSWORD, CPF, EMAIL, JWT, ASAAS_KEY, DB_PASS]


def _make_logger(fmt: str, monkeypatch) -> tuple[logging.Logger, io.StringIO]:
    monkeypatch.setenv("LOG_FORMAT", fmt)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging_config._make_formatter())
    handler.addFilter(logging_config._RequestContextFilter())
    handler.addFilter(SensitiveDataFilter())
    logger = logging.getLogger(f"test.masking.{fmt}.{id(stream)}")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    return logger, stream


def _raise_leaky_chain():
    try:
        raise ValueError(f"Illegal header value b'Bearer {RESEND_KEY}\\r\\n' password={PASSWORD}")
    except ValueError as inner:
        raise RuntimeError(
            f"falha ao enviar para {EMAIL} cpf {CPF} Authorization: Bearer {BEARER_TOKEN}",
            {"api_key": ASAAS_KEY, "jwt": JWT},
            f"postgresql://owner:{DB_PASS}@db.host/app",
        ) from inner


def _assert_no_secret(text: str) -> None:
    for secret in RAW_SECRETS:
        assert secret not in text, f"segredo vazou no log: {secret!r}\n{text}"


def test_json_formatter_masks_chained_traceback(monkeypatch):
    logger, stream = _make_logger("json", monkeypatch)
    try:
        _raise_leaky_chain()
    except RuntimeError:
        logger.exception("envio falhou")

    out = stream.getvalue()
    _assert_no_secret(out)
    payload = json.loads(out.strip())  # continua JSON válido, 1 linha
    assert payload["message"] == "envio falhou"
    tb = payload["exc_info"]
    assert "Traceback" in tb
    # a cadeia inteira foi renderizada (causa + efeito), só que mascarada
    assert "ValueError" in tb and "RuntimeError" in tb
    assert "direct cause" in tb
    assert "Bearer ***" in tb and "password=***" in tb


def test_text_formatter_masks_traceback(monkeypatch):
    logger, stream = _make_logger("text", monkeypatch)
    try:
        _raise_leaky_chain()
    except RuntimeError:
        logger.error("envio falhou", exc_info=True)
    out = stream.getvalue()
    _assert_no_secret(out)
    assert "Traceback" in out and "RuntimeError" in out


def test_exception_as_format_arg_is_masked(monkeypatch):
    logger, stream = _make_logger("json", monkeypatch)
    exc = RuntimeError(f"Illegal header value b'Bearer {RESEND_KEY}\\r\\n'")
    logger.warning("cost_alert email falhou err=%s", exc)
    out = stream.getvalue()
    _assert_no_secret(out)
    assert json.loads(out.strip())["message"].startswith("cost_alert email falhou err=")


def test_stack_info_is_masked():
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "msg", None, None)
    record.stack_info = f'Stack (most recent call last):\n  send(headers={{"Authorization": "Bearer {BEARER_TOKEN}"}}, senha="{PASSWORD}")'
    SensitiveDataFilter().filter(record)
    _assert_no_secret(record.stack_info)


def test_preexisting_exc_text_is_masked_and_exc_info_kept():
    """exc_info NÃO é descartado (Sentry precisa do objeto), só o texto é mascarado."""
    try:
        _raise_leaky_chain()
    except RuntimeError:
        import sys
        ei = sys.exc_info()
    record = logging.LogRecord("t", logging.ERROR, __file__, 1, "boom", None, ei)
    SensitiveDataFilter().filter(record)
    assert record.exc_info is ei
    assert record.exc_text
    _assert_no_secret(record.exc_text)
    # formatter padrão reaproveita o exc_text mascarado
    _assert_no_secret(logging.Formatter().format(record))


def test_mixin_masks_even_when_filter_absent():
    class F(MaskingFormatterMixin, logging.Formatter):
        pass
    try:
        _raise_leaky_chain()
    except RuntimeError:
        import sys
        ei = sys.exc_info()
    _assert_no_secret(F().formatException(ei))


def test_format_strings_are_preserved(monkeypatch):
    """Placeholders %s junto de chaves sensíveis não podem ser corrompidos."""
    logger, stream = _make_logger("text", monkeypatch)
    logger.info("token=%s password: %s url=postgresql://u:%s@h/db", "a", "b", "c")
    out = stream.getvalue()
    assert "--- Logging error ---" not in out
    assert "token=a" in out  # args sem padrão sensível seguem como antes


@pytest.mark.parametrize("text", [
    'File "app/x.py", line 3, in re_enable_notifications_for_user',
    "max_tokens=5 token_version=3",
    "basic configuration loaded",
    "walk 3f2a-uuid status Agendado",
])
def test_no_false_positives_on_common_text(text):
    assert _mask_string(text) == text


def test_mask_is_idempotent():
    once = _mask_string(f"Bearer {BEARER_TOKEN} password={PASSWORD} {RESEND_KEY}")
    assert _mask_string(once) == once


# ── Telefone BR/E.164 (sec-audit 2026-09-15: walk_emergency_service vazava o
# número do tutor/passeador para o Sentry via LOGGER.exception) ─────────────

TUTOR_PHONE_FORMATTED = "(71) 98888-7777"
TUTOR_PHONE_BARE = "71988887777"
TUTOR_PHONE_E164 = "+5571988887777"


def test_mask_string_masks_e164_phone_keeping_last_two_digits():
    masked = _mask_string(f"tutor_e164={TUTOR_PHONE_E164}")
    assert TUTOR_PHONE_E164 not in masked
    assert "988887777" not in masked
    assert "***77" in masked


def test_mask_string_masks_formatted_br_phone_keeping_last_two_digits():
    masked = _mask_string(f"telefone do tutor: {TUTOR_PHONE_FORMATTED}")
    assert TUTOR_PHONE_FORMATTED not in masked
    assert "988887777" not in masked
    assert "***77" in masked


def test_mask_string_masks_bare_br_phone_keeping_last_two_digits():
    masked = _mask_string(f"walker_e164 sem +55: {TUTOR_PHONE_BARE}")
    assert TUTOR_PHONE_BARE not in masked
    assert "***77" in masked


def test_phone_masking_does_not_break_cpf_and_email_masking():
    text = f"cpf {CPF} email {EMAIL} tel {TUTOR_PHONE_E164}"
    masked = _mask_string(text)
    assert CPF not in masked
    assert EMAIL not in masked
    assert TUTOR_PHONE_E164 not in masked


@pytest.mark.parametrize("text", [
    "walk 3f2a-uuid status Agendado",
    "max_tokens=5 token_version=3",
])
def test_phone_regex_does_not_flag_common_non_phone_text(text):
    assert _mask_string(text) == text


def test_cpf_is_not_mistaken_for_a_phone_number():
    # CPF continua mascarado do jeito de sempre ("***"), não vira "***77" de telefone.
    assert _mask_string("123.456.789-09") == "***"
