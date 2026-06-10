from datetime import datetime
from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.core.database import Base

class WalkerProfile(Base):
    __tablename__ = "walker_profiles"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), unique=True)
    full_name: Mapped[str] = mapped_column(String, default="")
    cpf: Mapped[str] = mapped_column(String, default="")
    phone: Mapped[str] = mapped_column(String, default="")
    birth_date: Mapped[str] = mapped_column(String, default="")
    city: Mapped[str] = mapped_column(String, default="")
    state: Mapped[str] = mapped_column(String, default="")
    experience: Mapped[str] = mapped_column(Text, default="")
    bio: Mapped[str] = mapped_column(Text, default="")
    profile_photo_url: Mapped[str | None] = mapped_column(String, nullable=True)
    rg: Mapped[str] = mapped_column(String, default="")
    document_url: Mapped[str | None] = mapped_column(String, nullable=True)
    identity_document_back_url: Mapped[str | None] = mapped_column(String, nullable=True)
    selfie_url: Mapped[str | None] = mapped_column(String, nullable=True)
    proof_of_address_url: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="pending")
    internal_notes: Mapped[str] = mapped_column(Text, default="")
    active_as_walker: Mapped[bool] = mapped_column(Boolean, default=False)
    # Passeador possui carro — requisito para receber Pet Tour (ver pet_tour_service).
    has_vehicle: Mapped[bool] = mapped_column(Boolean, default=False)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, default=datetime.utcnow, nullable=True)
    reviewed_by_admin_id: Mapped[str | None] = mapped_column(String, nullable=True)
    resubmission_requested_documents: Mapped[str] = mapped_column(Text, default="")

    user = relationship("User", back_populates="walker_profile")
