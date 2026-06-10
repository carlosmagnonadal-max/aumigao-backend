"""Notificação de novos contatos do site.

Hook plugável: hoje apenas registra em log. Quando o n8n (Sprint 18) ou um provedor
de e-mail estiver disponível, plugar AQUI o envio para negocios@aumigaowalk.com.br —
sem tocar na rota nem no formulário do site.
"""
import logging

LOGGER = logging.getLogger(__name__)


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
    # TODO(n8n/email): encaminhar para negocios@aumigaowalk.com.br quando a infra existir.
