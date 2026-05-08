from datetime import datetime
from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.core.database import Base

class TutorProfile(Base):
    __tablename__ = "tutor_profiles"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), unique=True)
    full_name: Mapped[str] = mapped_column(String, default="")
    cpf: Mapped[str] = mapped_column(String, default="")
    phone: Mapped[str] = mapped_column(String, default="")
    photo_url: Mapped[str | None] = mapped_column(String, nullable=True)
    cep: Mapped[str] = mapped_column(String, default="")
    street: Mapped[str] = mapped_column(String, default="")
    number: Mapped[str] = mapped_column(String, default="")
    complement: Mapped[str] = mapped_column(String, default="")
    neighborhood: Mapped[str] = mapped_column(String, default="")
    city: Mapped[str] = mapped_column(String, default="")
    state: Mapped[str] = mapped_column(String, default="")
    reference_point: Mapped[str] = mapped_column(String, default="")
    access_instructions: Mapped[str] = mapped_column(Text, default="")
    pickup_notes: Mapped[str] = mapped_column(Text, default="")
    preferred_method: Mapped[str] = mapped_column(String, default="Buscar em casa")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="tutor_profile")
