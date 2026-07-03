from datetime import datetime
from uuid import uuid4

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class TenantProductHighlight(Base):
    """Item da Vitrine de Destaques e Promoções do tenant (Fase 1 — demonstração).

    Curadoria de POUCOS produtos/serviços em destaque/promoção que o tenant exibe no
    app do tutor. NÃO é catálogo/estoque: o limite de itens ATIVOS por tenant é imposto
    no product_highlight_service (env PRODUCT_HIGHLIGHTS_MAX_ACTIVE, default 6).

    Diferencial do plano ENTERPRISE (gate por plano + toggle `product_highlights`).
    Fase 2 futura terá venda + entrega pelo passeador — nada de transação aqui.

    Preços em CENTAVOS (int) para evitar float. promo_price_cents, quando presente, é o
    preço promocional e DEVE ser < price_cents (validado no service).
    """

    __tablename__ = "tenant_product_highlights"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid4()))
    tenant_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("tenants.id"), nullable=True, index=True
    )
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    photo_url: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    price_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    promo_price_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
