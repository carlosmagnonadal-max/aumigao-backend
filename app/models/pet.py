from datetime import date, datetime
from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.core.database import Base

class Pet(Base):
    __tablename__ = "pets"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    tutor_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), index=True)
    tenant_id: Mapped[str | None] = mapped_column(String, ForeignKey("tenants.id"), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String)
    photo_url: Mapped[str | None] = mapped_column(String, nullable=True)
    species: Mapped[str] = mapped_column(String, default="Cachorro")
    sex: Mapped[str] = mapped_column(String, default="")
    breed: Mapped[str] = mapped_column(String, default="")
    size: Mapped[str] = mapped_column(String, default="")
    weight: Mapped[float | None] = mapped_column(Float, nullable=True)
    age: Mapped[int | None] = mapped_column(Integer, nullable=True)
    behavior_notes: Mapped[str] = mapped_column(Text, default="")
    is_social: Mapped[bool] = mapped_column(Boolean, default=True)
    afraid_of_noise: Mapped[bool] = mapped_column(Boolean, default=False)
    pulls_leash: Mapped[bool] = mapped_column(Boolean, default=False)
    can_walk_with_other_pets: Mapped[bool] = mapped_column(Boolean, default=False)
    is_neutered: Mapped[bool] = mapped_column(Boolean, default=False)
    allergies: Mapped[str] = mapped_column(Text, default="")
    medications: Mapped[str] = mapped_column(Text, default="")
    restrictions: Mapped[str] = mapped_column(Text, default="")
    health_notes: Mapped[str] = mapped_column(Text, default="")
    birth_date: Mapped["date | None"] = mapped_column(Date, nullable=True)
    chip_number: Mapped[str | None] = mapped_column(String, nullable=True)
    vet_name: Mapped[str | None] = mapped_column(String, nullable=True)
    vet_phone: Mapped[str | None] = mapped_column(String, nullable=True)
    emergency_contact: Mapped[str | None] = mapped_column(String, nullable=True)
    # Ficha rica (Perfil Vivo 2.0 — Fase A). microchip é distinto do chip_number
    # legado (0073): campo canônico da ficha rica. Dieta estruturada abaixo.
    microchip: Mapped[str | None] = mapped_column(String, nullable=True)
    diet_type: Mapped[str | None] = mapped_column(String, nullable=True)  # seca|umida|natural|mista|outro
    diet_brand: Mapped[str | None] = mapped_column(String, nullable=True)
    diet_line: Mapped[str | None] = mapped_column(String, nullable=True)
    diet_grams_per_meal: Mapped[int | None] = mapped_column(Integer, nullable=True)
    diet_meals_per_day: Mapped[int | None] = mapped_column(Integer, nullable=True)
    diet_meal_times: Mapped[str | None] = mapped_column(String, nullable=True)  # JSON simples (lista de horários)
    diet_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    tutor = relationship("User", back_populates="pets")
