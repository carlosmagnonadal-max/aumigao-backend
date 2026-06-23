from datetime import date, datetime
import sqlalchemy as sa
from sqlalchemy import Date, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column
from app.core.database import Base


class WalkerAvailabilityException(Base):
    """Exceção pontual à disponibilidade recorrente do passeador, por DATA.
    tenant_id=NULL → global (vale p/ todos os tenants).
    tenant_id=X   → escopo daquele tenant específico.
    kind=block (precede recorrente) | open (extra).
    Faixa start_time/end_time (HH:MM); NULL+NULL = dia inteiro."""

    __tablename__ = "walker_availability_exceptions"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    walker_user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), nullable=False, index=True)
    exception_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(8), nullable=False)
    start_time: Mapped[str | None] = mapped_column(String(5), nullable=True)
    end_time: Mapped[str | None] = mapped_column(String(5), nullable=True)
    tenant_id: Mapped[str | None] = mapped_column(String, ForeignKey("tenants.id"), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow, server_default=sa.func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow, server_default=sa.func.now(), onupdate=datetime.utcnow)
