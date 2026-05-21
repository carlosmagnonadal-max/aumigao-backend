from datetime import datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class LegalAcceptance(Base):
    __tablename__ = "legal_acceptances"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(String, index=True)
    user_role: Mapped[str] = mapped_column(String, index=True)
    terms_version: Mapped[str] = mapped_column(String, default="")
    privacy_version: Mapped[str] = mapped_column(String, default="")
    cancellation_version: Mapped[str] = mapped_column(String, default="")
    lgpd_version: Mapped[str] = mapped_column(String, default="")
    geolocation_version: Mapped[str] = mapped_column(String, default="")
    accepted_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
