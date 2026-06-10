"""Notificação de novos contatos do site.

O lead é SEMPRE persistido (na rota). Aqui, se houver envio configurado por env,
manda um e-mail para o time comercial; senão, apenas registra em log (no-op
gracioso). Liga quando você setar as credenciais — sem tocar na rota nem no
formulário.

Caminho principal: API HTTP do Resend (porta 443, não sofre bloqueio de portas
SMTP de saída comum em PaaS como o Railway). Fallback: SMTP, para outros
provedores.

Env (todas opcionais; sem elas = só log):
  # Resend (HTTP) — recomendado:
  RESEND_API_KEY (ou reaproveita SMTP_PASSWORD se for chave "re_" com SMTP_HOST resend)
  SMTP_FROM (remetente; precisa ser de domínio verificado no Resend)
  CONTACT_NOTIFICATION_TO (=contato@aumigaowalk.com.br)
  # SMTP (fallback):
  SMTP_HOST, SMTP_PORT (=587), SMTP_USER, SMTP_PASSWORD, SMTP_SSL (=false)
"""
import logging
import os
import smtplib
from email.message import EmailMessage

import httpx

LOGGER = logging.getLogger(__name__)

DEFAULT_CONTACT_TO = "contato@aumigaowalk.com.br"
RESEND_ENDPOINT = "https://api.resend.com/emails"


def _smtp_configured() -> bool:
    return bool(os.getenv("SMTP_HOST") and os.getenv("SMTP_USER") and os.getenv("SMTP_PASSWORD"))


def _resend_api_key() -> str | None:
    """Chave da API do Resend. Aceita RESEND_API_KEY explícita, ou reaproveita a
    SMTP_PASSWORD quando ela é uma chave do Resend (re_...) usada com SMTP_HOST
    do Resend — assim não é preciso duplicar a credencial."""
    explicit = os.getenv("RESEND_API_KEY")
    if explicit:
        return explicit
    smtp_pw = os.getenv("SMTP_PASSWORD", "")
    if smtp_pw.startswith("re_") and "resend.com" in os.getenv("SMTP_HOST", ""):
        return smtp_pw
    return None


def _subject(contact) -> str:
    return f"[Aumigão] Novo contato do site — {contact.interest or 'sem assunto'}"


def _body_text(contact) -> str:
    linhas = [
        f"Nome:            {contact.name or '-'}",
        f"Empresa:         {contact.company or '-'}",
        f"E-mail:          {contact.email or '-'}",
        f"Telefone:        {contact.phone or '-'}",
        f"Cidade:          {contact.city or '-'}",
        f"Tipo de negocio: {contact.business_type or '-'}",
        f"Interesse:       {contact.interest or '-'}",
        "",
        "Mensagem:",
        (contact.message or "-"),
        "",
        f"— Aumigao Walk · lead #{contact.id}",
    ]
    return "\n".join(linhas)


def build_contact_email(contact) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = _subject(contact)
    msg["From"] = os.getenv("SMTP_FROM") or os.getenv("SMTP_USER") or DEFAULT_CONTACT_TO
    msg["To"] = os.getenv("CONTACT_NOTIFICATION_TO", DEFAULT_CONTACT_TO)
    if (contact.email or "").strip():
        msg["Reply-To"] = contact.email
    msg.set_content(_body_text(contact))
    return msg


def _send_via_resend(contact, api_key: str) -> None:
    to_addr = os.getenv("CONTACT_NOTIFICATION_TO", DEFAULT_CONTACT_TO)
    from_addr = os.getenv("SMTP_FROM") or DEFAULT_CONTACT_TO
    payload = {
        "from": from_addr,
        "to": [to_addr],
        "subject": _subject(contact),
        "text": _body_text(contact),
    }
    if (contact.email or "").strip():
        payload["reply_to"] = contact.email

    resp = httpx.post(
        RESEND_ENDPOINT,
        headers={"Authorization": f"Bearer {api_key}"},
        json=payload,
        timeout=10,
    )
    if resp.status_code >= 400:
        # Loga o corpo do erro do Resend para diagnóstico (domínio não verificado,
        # remetente inválido, chave errada, etc.) sem quebrar o intake.
        LOGGER.error(
            "Resend API recusou envio (status=%s): %s — from=%s to=%s contact_id=%s",
            resp.status_code, resp.text[:600], from_addr, to_addr, contact.id,
        )
        return
    LOGGER.info("e-mail de contato enviado via Resend para %s contact_id=%s", to_addr, contact.id)


def _send_via_smtp(contact) -> None:
    msg = build_contact_email(contact)
    host = os.getenv("SMTP_HOST")
    port = int(os.getenv("SMTP_PORT", "587"))
    user = os.getenv("SMTP_USER")
    password = os.getenv("SMTP_PASSWORD")
    use_ssl = os.getenv("SMTP_SSL", "false").strip().lower() in {"1", "true", "yes", "on"}

    if use_ssl:
        with smtplib.SMTP_SSL(host, port, timeout=15) as server:
            server.login(user, password)
            server.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=15) as server:
            server.starttls()
            server.login(user, password)
            server.send_message(msg)
    LOGGER.info("e-mail de contato enviado via SMTP para %s contact_id=%s", msg["To"], contact.id)


def notify_new_contact(contact) -> None:
    LOGGER.info(
        "novo contato do site recebido",
        extra={
            "contact_id": contact.id,
            "email": contact.email,
            "interest": contact.interest,
            "business_type": contact.business_type,
        },
    )

    api_key = _resend_api_key()
    try:
        if api_key:
            _send_via_resend(contact, api_key)
        elif _smtp_configured():
            _send_via_smtp(contact)
        else:
            LOGGER.info("envio nao configurado; contato apenas persistido (sem e-mail). contact_id=%s", contact.id)
    except Exception:  # noqa: BLE001 - envio nunca pode quebrar o intake (lead ja persistido)
        LOGGER.exception("falha ao enviar e-mail de contato (lead ja persistido) contact_id=%s", contact.id)
