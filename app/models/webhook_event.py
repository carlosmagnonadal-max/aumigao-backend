"""Dedup persistente de webhooks do provedor de pagamento (Asaas).

Cada webhook do Asaas traz um `id` de EVENTO único (ex.: "evt_..."). Sem uma
chave de evento gravada, um reenvio (janela de falha parcial, retry do provedor)
poderia reaplicar um efeito financeiro (conceder crédito, confirmar pagamento) 2x.

Esta tabela grava o event_id com UNIQUE. No início do handler fazemos um
INSERT-if-not-exists: se o evento já foi processado, o handler retorna 200 sem
reaplicar efeito. Escopo GLOBAL (o webhook processa qualquer tenant sob
rls_tenant="*") — sem tenant_id.
"""
from datetime import datetime
from uuid import uuid4

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class WebhookEvent(Base):
    __tablename__ = "webhook_events"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid4()))
    # ID do evento no provedor (Asaas). UNIQUE: a 2ª tentativa de gravar o mesmo
    # event_id viola a constraint → tratado como duplicata (no-op).
    event_id: Mapped[str] = mapped_column(String, nullable=False, unique=True, index=True)
    provider: Mapped[str] = mapped_column(String, nullable=False, default="asaas")
    event_type: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
