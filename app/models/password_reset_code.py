"""Modelo para códigos de recuperação de senha (fluxo mobile-friendly).

O código de 6 dígitos gerado no fluxo forgot-password é armazenado como hash
(pbkdf2_sha256), nunca em claro. TTL de 15 min, máximo de 5 tentativas por código.
"""
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class PasswordResetCode(Base):
    __tablename__ = "password_reset_codes"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), index=True)
    code_hash: Mapped[str] = mapped_column(String)  # hash pbkdf2_sha256, nunca plaintext
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
