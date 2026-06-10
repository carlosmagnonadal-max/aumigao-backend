"""Notificação de novos contatos do site.

O lead é SEMPRE persistido (na rota). Aqui, se houver SMTP configurado por env,
envia um e-mail para o time comercial; senão, apenas registra em log (no-op
gracioso). Mesma filosofia do storage: liga quando você setar as credenciais —
sem tocar na rota nem no formulário.

Env (todas opcionais; sem elas = só log):
  SMTP_HOST, SMTP_PORT (=587), SMTP_USER, SMTP_PASSWORD,
  SMTP_FROM (=SMTP_USER), SMTP_SSL (=false),
  CONTACT_NOTIFICATION_TO (=contato@aumigaowalk.com.br)
"""
import logging
import os
import smtplib
from email.message import EmailMessage

LOGGER = logging.getLogger(__name__)

DEFAULT_CONTACT_TO = "contato@aumigaowalk.com.br"


def _smtp_configured() -> bool:
    return bool(os.getenv("SMTP_HOST") and os.getenv("SMTP_USER") and os.getenv("SMTP_PASSWORD"))


def build_contact_email(contact) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = f"[Aumigão] Novo contato do site — {contact.interest or 'sem assunto'}"
    msg["From"] = os.getenv("SMTP_FROM") or os.getenv("SMTP_USER") or DEFAULT_CONTACT_TO
    msg["To"] = os.getenv("CONTACT_NOTIFICATION_TO", DEFAULT_CONTACT_TO)
    if (contact.email or "").strip():
        msg["Reply-To"] = contact.email
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
    msg.set_content("\n".join(linhas))
    return msg


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
    if not _smtp_configured():
        LOGGER.info("SMTP nao configurado; contato apenas persistido (sem e-mail). contact_id=%s", contact.id)
        return

    try:
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
        LOGGER.info("e-mail de contato enviado para %s contact_id=%s", msg["To"], contact.id)
    except Exception:  # noqa: BLE001 - envio nunca pode quebrar o intake (lead ja persistido)
        LOGGER.exception("falha ao enviar e-mail de contato (lead ja persistido) contact_id=%s", contact.id)
