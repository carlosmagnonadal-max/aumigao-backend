from datetime import datetime
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.core.database import Base

class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    email: Mapped[str] = mapped_column(String, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String)
    tenant_id: Mapped[str | None] = mapped_column(String, ForeignKey("tenants.id"), nullable=True, index=True)
    full_name: Mapped[str] = mapped_column(String, default="")
    role: Mapped[str] = mapped_column(String, default="tutor")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # SEC (account-takeover Apple): identificador ESTÁVEL do usuário na Apple ("sub" do
    # identity_token, sempre presente e assinado). É a âncora de identidade do Apple
    # Sign-In — nunca confiar no e-mail client-supplied. Nullable pois só usuários que
    # logaram via Apple têm valor; unique/index para busca e para impedir 2 contas no
    # mesmo sub.
    apple_sub: Mapped[str | None] = mapped_column(String, nullable=True, unique=True, index=True)
    # B-ALT-011 (passo 2b): versao da sessao. O access token carrega "ver" = este valor;
    # incrementar (na troca/reset de senha) revoga TODAS as sessoes antigas do usuario.
    token_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    # B2: flag de troca obrigatoria de senha no 1o login. Setada True ao criar conta
    # admin via POST /admin/accounts; zerada apos troca bem-sucedida via /auth/change-password.
    must_change_password: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false", default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    tutor_profile = relationship("TutorProfile", back_populates="user", uselist=False)
    walker_profile = relationship("WalkerProfile", back_populates="user", uselist=False)
    pets = relationship("Pet", back_populates="tutor")
    walks = relationship("Walk", back_populates="tutor", foreign_keys="Walk.tutor_id")
