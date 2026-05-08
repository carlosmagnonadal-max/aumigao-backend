from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class WalkerMonitoringAlert(Base):
    __tablename__ = "walker_monitoring_alerts"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    walker_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), index=True)
    alert_type: Mapped[str] = mapped_column(String, index=True)
    severity: Mapped[str] = mapped_column(String, default="low", index=True)
    title: Mapped[str] = mapped_column(String)
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String, default="open", index=True)
    source: Mapped[str] = mapped_column(String, default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    reviewed_by_admin_id: Mapped[str | None] = mapped_column(String, ForeignKey("users.id"), nullable=True)
    admin_notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    walker = relationship("User", foreign_keys=[walker_id])
    reviewed_by_admin = relationship("User", foreign_keys=[reviewed_by_admin_id])
