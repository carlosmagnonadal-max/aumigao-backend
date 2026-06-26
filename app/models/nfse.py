"""Model NFS-e — nota fiscal de serviço emitida via Asaas.

Dormente por padrão (NFS_E_ENABLED=false). Nenhum dado é persistido
enquanto a flag estiver desligada.
"""
from datetime import datetime
from uuid import uuid4

from sqlalchemy import DateTime, ForeignKey, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


def _uuid() -> str:
    return str(uuid4())


# ---- Status constants -------------------------------------------------------
NFSE_SCHEDULED = "scheduled"
NFSE_SYNCHRONIZED = "synchronized"
NFSE_AUTHORIZED = "authorized"
NFSE_CANCELED = "canceled"
NFSE_ERROR = "error"


class Nfse(Base):
    __tablename__ = "nfse"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    # asaas_payment_id é a chave de idempotência: garante que não emitimos 2x para o mesmo pagamento.
    asaas_payment_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    subscription_id: Mapped[str | None] = mapped_column(String, nullable=True)
    asaas_invoice_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    # 'saas' = mensalidade de plataforma | 'commission' = comissão de passeio (futuro)
    service_type: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default=NFSE_SCHEDULED)
    value: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    nfse_number: Mapped[str | None] = mapped_column(String, nullable=True)
    pdf_url: Mapped[str | None] = mapped_column(String, nullable=True)
    xml_url: Mapped[str | None] = mapped_column(String, nullable=True)
    validation_code: Mapped[str | None] = mapped_column(String, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    external_reference: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
