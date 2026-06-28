from datetime import datetime
from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, event
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.core.database import Base
from app.core.pii_crypto import EncryptedString, blind_index

class WalkerProfile(Base):
    __tablename__ = "walker_profiles"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), unique=True)
    full_name: Mapped[str] = mapped_column(String, default="")
    cpf: Mapped[str] = mapped_column(EncryptedString, default="")
    cpf_bidx: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    phone: Mapped[str] = mapped_column(String, default="")
    birth_date: Mapped[str] = mapped_column(String, default="")
    city: Mapped[str] = mapped_column(String, default="")
    state: Mapped[str] = mapped_column(String, default="")
    experience: Mapped[str] = mapped_column(Text, default="")
    bio: Mapped[str] = mapped_column(Text, default="")
    profile_photo_url: Mapped[str | None] = mapped_column(String, nullable=True)
    rg: Mapped[str] = mapped_column(EncryptedString, default="")
    document_url: Mapped[str | None] = mapped_column(String, nullable=True)
    identity_document_back_url: Mapped[str | None] = mapped_column(String, nullable=True)
    selfie_url: Mapped[str | None] = mapped_column(String, nullable=True)
    proof_of_address_url: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="pending")
    internal_notes: Mapped[str] = mapped_column(Text, default="")
    active_as_walker: Mapped[bool] = mapped_column(Boolean, default=False)
    # Passeador possui carro — requisito para receber Pet Tour (ver pet_tour_service).
    has_vehicle: Mapped[bool] = mapped_column(Boolean, default=False)
    # Wave 5 — porte máximo de cão que o passeador aceita ("Pequeno"|"Médio"|"Grande").
    # Default PERMISSIVO "Grande": aceita todos os portes (zero regressão até configurarem).
    max_dog_size: Mapped[str] = mapped_column(String, default="Grande", server_default="Grande", nullable=False)
    # WK-02: presença real. is_online é input do matching (WK-10); last_seen_at permite
    # política de offline por TTL no futuro. Default offline.
    is_online: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, default=datetime.utcnow, nullable=True)
    reviewed_by_admin_id: Mapped[str | None] = mapped_column(String, nullable=True)
    resubmission_requested_documents: Mapped[str] = mapped_column(Text, default="")
    # ID da carteira Asaas do walker para split real (Fase B — dormente até PAYMENT_MODE=asaas_live).
    # Configurável via PATCH /admin/walkers/{user_id}/wallet com permissão finance.manage.
    asaas_wallet_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # FIX 6a: chave Pix cadastrada pelo próprio walker para receber saques.
    # Nullable — walker pode ainda não ter configurado.
    pix_key: Mapped[str | None] = mapped_column(String, nullable=True)
    # Background Check Fase 0 — status agregado das certidoes de antecedentes.
    # none|submitted|partial|verified|flagged. Default "none" => ZERO efeito ate ligarem a flag.
    background_check_status: Mapped[str] = mapped_column(
        String, default="none", server_default="none", nullable=False
    )
    background_verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Consentimento LGPD (base legal p/ dado sensivel) — data + versao do texto aceito.
    background_consent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    background_consent_version: Mapped[str | None] = mapped_column(String, nullable=True)

    # Trilha de devido processo para status restritivos (blocked/rejected durante atividade).
    # Populadas sempre que a API transiciona para um estado restritivo.
    # suspension_reason = motivo registrado pelo admin (obrigatorio via API).
    # status_changed_by = user_id do admin que fez a alteracao.
    # status_changed_at = timestamp UTC da alteracao.
    suspension_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    status_changed_by: Mapped[str | None] = mapped_column(String, nullable=True)
    status_changed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    user = relationship("User", back_populates="walker_profile")


# ---------------------------------------------------------------------------
# Event listeners: preenche cpf_bidx automaticamente antes de insert/update.
# target.cpf é texto puro aqui (TypeDecorator cifra no bind_param).
# RG não tem blind index — não é usado em pesquisa/unicidade.
# ---------------------------------------------------------------------------

@event.listens_for(WalkerProfile, "before_insert")
@event.listens_for(WalkerProfile, "before_update")
def _walker_set_cpf_bidx(mapper, connection, target):  # noqa: ARG001
    target.cpf_bidx = blind_index(target.cpf)
