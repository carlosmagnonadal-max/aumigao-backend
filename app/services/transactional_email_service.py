"""Serviço de e-mails transacionais (reset de senha, boas-vindas, etc.).

Usa o mesmo mecanismo de transporte Resend/SMTP do contact_notification_service.
Remetente configurado por TRANSACTIONAL_EMAIL_FROM (fallback: SMTP_FROM, depois
noreply@aumigaowalk.com.br).

Env:
  RESEND_API_KEY          — chave Resend (ou re_... em SMTP_PASSWORD com SMTP_HOST resend)
  TRANSACTIONAL_EMAIL_FROM — remetente (domínio verificado no Resend)
  SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, SMTP_SSL — fallback SMTP
"""
from __future__ import annotations

import logging
import os
import smtplib
from email.message import EmailMessage
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

LOGGER = logging.getLogger(__name__)
RESEND_ENDPOINT = "https://api.resend.com/emails"
DEFAULT_FROM = "noreply@aumigaowalk.com.br"


# --------------------------------------------------------------------------- #
# helpers de transporte (espelham contact_notification_service)                #
# --------------------------------------------------------------------------- #

def _from_addr() -> str:
    return (
        os.getenv("TRANSACTIONAL_EMAIL_FROM")
        or os.getenv("SMTP_FROM")
        or os.getenv("SMTP_USER")
        or DEFAULT_FROM
    )


def _resend_api_key() -> str | None:
    explicit = os.getenv("RESEND_API_KEY")
    if explicit:
        return explicit
    smtp_pw = os.getenv("SMTP_PASSWORD", "")
    if smtp_pw.startswith("re_") and "resend.com" in os.getenv("SMTP_HOST", ""):
        return smtp_pw
    return None


def _smtp_configured() -> bool:
    return bool(os.getenv("SMTP_HOST") and os.getenv("SMTP_USER") and os.getenv("SMTP_PASSWORD"))


def _send_via_resend(to: str, subject: str, body_text: str, body_html: str | None = None) -> None:
    api_key = _resend_api_key()
    if not api_key:
        raise RuntimeError("Resend API key não configurada")
    payload: dict = {
        "from": _from_addr(),
        "to": [to],
        "subject": subject,
        "text": body_text,
    }
    if body_html:
        payload["html"] = body_html
    resp = httpx.post(
        RESEND_ENDPOINT,
        headers={"Authorization": f"Bearer {api_key}"},
        json=payload,
        timeout=10,
    )
    if resp.status_code >= 500:
        # Falha do servidor Resend — alerta crítico para ops; e-mail NÃO enviado.
        LOGGER.critical(
            "[ALERTA] Resend com falha de servidor (status=%s): %s — to=%s",
            resp.status_code, resp.text[:600], to,
        )
        return
    if resp.status_code >= 400:
        # Falha de configuração (domínio não verificado, chave inválida, destinatário rejeitado).
        LOGGER.error(
            "Resend recusou e-mail transacional (status=%s): %s — to=%s",
            resp.status_code, resp.text[:600], to,
        )
        return
    LOGGER.info("e-mail transacional enviado via Resend para %s subject=%r", to, subject)


def _send_via_smtp(to: str, subject: str, body_text: str) -> None:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = _from_addr()
    msg["To"] = to
    msg.set_content(body_text)

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
    LOGGER.info("e-mail transacional enviado via SMTP para %s subject=%r", to, subject)


def _transactional_emails_enabled(db: "Session | None", tenant_id: str | None) -> bool:
    """Retorna False se transactional_emails estiver desabilitado para o tenant."""
    if db is None or not tenant_id:
        return True  # indeterminavel → envia
    try:
        from app.models.tenant import Tenant  # import local para evitar ciclo
        from app.services.tenant_plan_service import tenant_feature_enabled
        tenant = db.get(Tenant, tenant_id)
        if not tenant:
            return True
        return tenant_feature_enabled(tenant, db, "transactional_emails")
    except Exception:
        return True  # nunca falha silenciosamente


def _send_email(to: str, subject: str, body_text: str, body_html: str | None = None) -> None:
    """Envia e-mail escolhendo Resend → SMTP → log-only.

    Nunca lança exceção — quem chama decide se ignora ou relança.
    """
    api_key = _resend_api_key()
    try:
        if api_key:
            _send_via_resend(to, subject, body_text, body_html)
        elif _smtp_configured():
            _send_via_smtp(to, subject, body_text)
        else:
            LOGGER.info(
                "envio de e-mail transacional não configurado; apenas log. to=%s subject=%r",
                to, subject,
            )
    except Exception:
        LOGGER.exception("falha ao enviar e-mail transacional to=%s subject=%r", to, subject)


# --------------------------------------------------------------------------- #
# templates                                                                    #
# --------------------------------------------------------------------------- #

def send_password_reset_email(to: str, code: str, user_name: str, *, db: "Session | None" = None, tenant_id: str | None = None) -> None:
    """Envia o código de 6 dígitos para reset de senha.

    Fire-safe: captura toda exceção internamente — a rota nunca falha por causa do e-mail.
    """
    if not _transactional_emails_enabled(db, tenant_id):
        return
    name = (user_name or "").split()[0] or "Usuário"
    subject = "[Aumigão] Código para redefinir sua senha"
    body_text = (
        f"Olá, {name}!\n\n"
        f"Recebemos uma solicitação para redefinir a senha da sua conta no Aumigão.\n\n"
        f"Seu código de verificação é:\n\n"
        f"    {code}\n\n"
        f"Este código é válido por 15 minutos e pode ser usado apenas uma vez.\n\n"
        f"Se você não solicitou a redefinição de senha, ignore este e-mail — "
        f"sua senha permanece a mesma.\n\n"
        f"— Equipe Aumigão 🐾"
    )
    body_html = (
        f"<p>Olá, <strong>{name}</strong>!</p>"
        f"<p>Recebemos uma solicitação para redefinir a senha da sua conta no Aumigão.</p>"
        f"<p>Seu código de verificação é:</p>"
        f'<p style="font-size:2em;font-weight:bold;letter-spacing:0.15em;'
        f'background:#f4f4f4;padding:12px 24px;border-radius:8px;display:inline-block;">'
        f"{code}</p>"
        f"<p>Este código é válido por <strong>15 minutos</strong> e pode ser usado apenas uma vez.</p>"
        f"<p>Se você não solicitou a redefinição de senha, ignore este e-mail — "
        f"sua senha permanece a mesma.</p>"
        f"<p>— Equipe Aumigão 🐾</p>"
    )
    _send_email(to, subject, body_text, body_html)


def send_welcome_email(to: str, user_name: str, *, db: "Session | None" = None, tenant_id: str | None = None) -> None:
    """Envia boas-vindas após cadastro.

    Fire-safe: captura toda exceção internamente — a rota nunca falha por causa do e-mail.
    """
    if not _transactional_emails_enabled(db, tenant_id):
        return
    name = (user_name or "").split()[0] or "Usuário"
    subject = "Bem-vindo(a) ao Aumigão! 🐾"
    body_text = (
        f"Olá, {name}!\n\n"
        f"Seja muito bem-vindo(a) ao Aumigão — a plataforma que cuida dos pets "
        f"com carinho e responsabilidade.\n\n"
        f"Próximos passos para começar:\n"
        f"  1. Cadastre seu pet no app\n"
        f"  2. Agende o primeiro passeio\n"
        f"  3. Acompanhe tudo em tempo real\n\n"
        f"Qualquer dúvida, estamos aqui para ajudar!\n\n"
        f"— Equipe Aumigão 🐾"
    )
    body_html = (
        f"<p>Olá, <strong>{name}</strong>!</p>"
        f"<p>Seja muito bem-vindo(a) ao <strong>Aumigão</strong> — a plataforma que cuida "
        f"dos pets com carinho e responsabilidade.</p>"
        f"<p><strong>Próximos passos para começar:</strong></p>"
        f"<ol>"
        f"<li>Cadastre seu pet no app</li>"
        f"<li>Agende o primeiro passeio</li>"
        f"<li>Acompanhe tudo em tempo real</li>"
        f"</ol>"
        f"<p>Qualquer dúvida, estamos aqui para ajudar!</p>"
        f"<p>— Equipe Aumigão 🐾</p>"
    )
    _send_email(to, subject, body_text, body_html)
