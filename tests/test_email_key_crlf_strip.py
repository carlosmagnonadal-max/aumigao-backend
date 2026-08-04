"""Regressão 2026-08-04: secret gravado via pipe do PowerShell chega com CRLF no
final; header Authorization com \r\n é rejeitado pelo httpx (LocalProtocolError)
ANTES do envio — e-mail nunca sai. A leitura da chave deve fazer strip().
"""
from app.services import contact_notification_service as contact
from app.services import transactional_email_service as transactional


def test_transactional_resend_key_strips_crlf(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "")
    monkeypatch.setenv("SMTP_PASSWORD", "re_chave_teste\r\n")
    monkeypatch.setenv("SMTP_HOST", "smtp.resend.com")
    assert transactional._resend_api_key() == "re_chave_teste"


def test_transactional_explicit_key_strips_whitespace(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "  re_explicita \n")
    assert transactional._resend_api_key() == "re_explicita"


def test_contact_resend_key_strips_crlf(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "")
    monkeypatch.setenv("SMTP_PASSWORD", "re_chave_teste\r\n")
    monkeypatch.setenv("SMTP_HOST", "smtp.resend.com")
    assert contact._resend_api_key() == "re_chave_teste"
