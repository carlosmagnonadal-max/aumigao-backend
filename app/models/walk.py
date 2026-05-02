from datetime import datetime
from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.core.database import Base

class Walk(Base):
    __tablename__ = "walks"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    tutor_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), index=True)
    walker_id: Mapped[str | None] = mapped_column(String, ForeignKey("users.id"), nullable=True)
    pet_id: Mapped[str] = mapped_column(String, ForeignKey("pets.id"))
    scheduled_date: Mapped[str] = mapped_column(String)
    duration_minutes: Mapped[int] = mapped_column(Integer)
    price: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String, default="Agendado")
    pickup_method: Mapped[str] = mapped_column(String, default="Buscar em casa")
    address_snapshot: Mapped[str] = mapped_column(Text, default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    tutor = relationship("User", back_populates="walks", foreign_keys=[tutor_id])
