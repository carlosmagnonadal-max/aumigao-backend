"""product_highlight_service.py — Vitrine de Destaques e Promoções do tenant (Fase 1).

Curadoria de POUCOS produtos/serviços em destaque/promoção que o tenant exibe no app do
tutor (demonstração, SEM transação nesta fase). NÃO é catálogo/estoque: há um LIMITE de
itens ATIVOS por tenant (env PRODUCT_HIGHLIGHTS_MAX_ACTIVE, default 6 — mesmo padrão de
env do free_plan_walk_cap: lido no uso, valor inválido/não-positivo cai no default).

Regras de validação (impostas aqui, no service, não na rota):
  - title obrigatório, <= 120 (trim);
  - description opcional, <= 500 (trim → None se vazio);
  - promo_price_cents < price_cents quando AMBOS presentes (senão 422);
  - price/promo não-negativos;
  - exceder o limite de ativos no create/activate → 422 com mensagem clara.

Gating (toggle + Enterprise) NÃO é responsabilidade deste service — fica nas rotas
(product_highlights.py), no padrão do Perfil Vivo.
"""
from __future__ import annotations

import os
from datetime import datetime
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.tenant_product_highlight import TenantProductHighlight

# Limite default de itens ATIVOS por tenant (env PRODUCT_HIGHLIGHTS_MAX_ACTIVE, default 6).
_DEFAULT_MAX_ACTIVE = 6

# Limites de tamanho (coerentes com a migration 0090/0095 / contrato).
TITLE_MAX = 120
DESCRIPTION_MAX = 500
PHOTO_URL_MAX = 2000
PRODUCT_URL_MAX = 2000


def product_highlights_max_active() -> int:
    """Máximo de destaques ATIVOS por tenant (env PRODUCT_HIGHLIGHTS_MAX_ACTIVE, default 6).

    Valor inválido/não-positivo cai no default (não desliga o limite por engano de config).
    Lido no momento do uso (não import-time) para ser testável via env override.
    """
    raw = os.getenv("PRODUCT_HIGHLIGHTS_MAX_ACTIVE", str(_DEFAULT_MAX_ACTIVE))
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return _DEFAULT_MAX_ACTIVE
    return value if value > 0 else _DEFAULT_MAX_ACTIVE


# ---------------------------------------------------------------------------
# Validação / normalização
# ---------------------------------------------------------------------------

def _clean_str(value: str | None, *, max_len: int, field: str, required: bool) -> str | None:
    """Trim + validação de tamanho. required → 422 se vazio; opcional → None se vazio."""
    text = (value or "").strip()
    if not text:
        if required:
            raise HTTPException(status_code=422, detail=f"{field} é obrigatório.")
        return None
    if len(text) > max_len:
        raise HTTPException(
            status_code=422, detail=f"{field} deve ter no máximo {max_len} caracteres."
        )
    return text


def _validate_prices(price_cents: int | None, promo_price_cents: int | None) -> None:
    """price/promo não-negativos; promo < price quando AMBOS presentes (422)."""
    for label, cents in (("price_cents", price_cents), ("promo_price_cents", promo_price_cents)):
        if cents is not None and cents < 0:
            raise HTTPException(status_code=422, detail=f"{label} não pode ser negativo.")
    if (
        price_cents is not None
        and promo_price_cents is not None
        and promo_price_cents >= price_cents
    ):
        raise HTTPException(
            status_code=422,
            detail="promo_price_cents deve ser menor que price_cents.",
        )


def _validate_product_url(url: str | None) -> str | None:
    """Valida e normaliza o link do produto.

    - None / vazio → None (campo opcional).
    - Presente: strip, deve começar com http:// ou https:// (422 caso contrário).
    - Máximo PRODUCT_URL_MAX caracteres.
    """
    if url is None:
        return None
    url = url.strip()
    if not url:
        return None
    if len(url) > PRODUCT_URL_MAX:
        raise HTTPException(
            status_code=422,
            detail=f"product_url deve ter no máximo {PRODUCT_URL_MAX} caracteres.",
        )
    if not (url.startswith("http://") or url.startswith("https://")):
        raise HTTPException(
            status_code=422,
            detail="product_url deve começar com http:// ou https://.",
        )
    return url


def _count_active(db: Session, tenant_id: str, *, exclude_id: str | None = None) -> int:
    q = db.query(TenantProductHighlight).filter(
        TenantProductHighlight.tenant_id == tenant_id,
        TenantProductHighlight.is_active.is_(True),
    )
    if exclude_id is not None:
        q = q.filter(TenantProductHighlight.id != exclude_id)
    return q.count()


def _enforce_active_limit(db: Session, tenant_id: str, *, exclude_id: str | None = None) -> None:
    """422 se o tenant já tem >= limite de itens ATIVOS (fora o exclude_id)."""
    limit = product_highlights_max_active()
    if _count_active(db, tenant_id, exclude_id=exclude_id) >= limit:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Limite de {limit} destaques ativos atingido. Desative um item antes "
                "de ativar/criar outro."
            ),
        )


# ---------------------------------------------------------------------------
# Serialização
# ---------------------------------------------------------------------------

def to_admin_dict(h: TenantProductHighlight) -> dict:
    """Item completo (visão admin — inclui is_active/sort_order/timestamps)."""
    return {
        "id": h.id,
        "tenant_id": h.tenant_id,
        "title": h.title,
        "description": h.description,
        "photo_url": h.photo_url,
        "product_url": h.product_url,
        "price_cents": h.price_cents,
        "promo_price_cents": h.promo_price_cents,
        "is_active": bool(h.is_active),
        "sort_order": h.sort_order,
        "created_at": h.created_at.isoformat() if h.created_at else None,
        "updated_at": h.updated_at.isoformat() if h.updated_at else None,
    }


def to_public_dict(h: TenantProductHighlight) -> dict:
    """Item público (app do tutor) — campos sanitizados, sem internals do tenant.

    Deriva has_promo e effective_price_cents (promo se houver, senão price) para o
    cliente não precisar recomputar. NÃO expõe is_active (só ativos chegam aqui).
    """
    has_promo = h.promo_price_cents is not None
    effective = h.promo_price_cents if has_promo else h.price_cents
    return {
        "id": h.id,
        "title": h.title,
        "description": h.description,
        "photo_url": h.photo_url,
        "product_url": h.product_url,
        "price_cents": h.price_cents,
        "promo_price_cents": h.promo_price_cents,
        "has_promo": has_promo,
        "effective_price_cents": effective,
        "sort_order": h.sort_order,
    }


# ---------------------------------------------------------------------------
# Consultas
# ---------------------------------------------------------------------------

def list_for_admin(db: Session, tenant_id: str) -> list[TenantProductHighlight]:
    """Todos os destaques do tenant (inclui inativos), ordenados por sort_order, id."""
    return (
        db.query(TenantProductHighlight)
        .filter(TenantProductHighlight.tenant_id == tenant_id)
        .order_by(TenantProductHighlight.sort_order.asc(), TenantProductHighlight.created_at.asc())
        .all()
    )


def list_active_public(db: Session, tenant_id: str) -> list[TenantProductHighlight]:
    """Só os destaques ATIVOS do tenant, ordenados por sort_order (app do tutor)."""
    return (
        db.query(TenantProductHighlight)
        .filter(
            TenantProductHighlight.tenant_id == tenant_id,
            TenantProductHighlight.is_active.is_(True),
        )
        .order_by(TenantProductHighlight.sort_order.asc(), TenantProductHighlight.created_at.asc())
        .all()
    )


def get_owned(db: Session, tenant_id: str, highlight_id: str) -> TenantProductHighlight | None:
    return (
        db.query(TenantProductHighlight)
        .filter(
            TenantProductHighlight.id == highlight_id,
            TenantProductHighlight.tenant_id == tenant_id,
        )
        .first()
    )


# ---------------------------------------------------------------------------
# Mutações (o caller comita)
# ---------------------------------------------------------------------------

def create_highlight(
    db: Session,
    tenant_id: str,
    *,
    title: str,
    description: str | None = None,
    photo_url: str | None = None,
    product_url: str | None = None,
    price_cents: int | None = None,
    promo_price_cents: int | None = None,
    is_active: bool = True,
    sort_order: int = 0,
) -> TenantProductHighlight:
    """Cria um destaque. 422 se exceder o limite de ativos (quando is_active=True)."""
    title = _clean_str(title, max_len=TITLE_MAX, field="title", required=True)
    description = _clean_str(description, max_len=DESCRIPTION_MAX, field="description", required=False)
    photo_url = _clean_str(photo_url, max_len=PHOTO_URL_MAX, field="photo_url", required=False)
    product_url = _validate_product_url(product_url)
    _validate_prices(price_cents, promo_price_cents)

    if is_active:
        _enforce_active_limit(db, tenant_id)

    now = datetime.utcnow()
    highlight = TenantProductHighlight(
        id=str(uuid4()),
        tenant_id=tenant_id,
        title=title,
        description=description,
        photo_url=photo_url,
        product_url=product_url,
        price_cents=price_cents,
        promo_price_cents=promo_price_cents,
        is_active=is_active,
        sort_order=sort_order or 0,
        created_at=now,
        updated_at=now,
    )
    db.add(highlight)
    db.flush()
    return highlight


def update_highlight(
    db: Session,
    highlight: TenantProductHighlight,
    *,
    fields: dict,
) -> TenantProductHighlight:
    """Aplica parcialmente `fields` (apenas as chaves presentes) e valida.

    Reavalia preços com o estado FINAL (mistura de valores novos + existentes) e, se a
    atualização ATIVAR um item que estava inativo, impõe o limite de ativos (excluindo
    o próprio item da contagem).
    """
    # Estado final de preço (para validar promo < price com valores misturados).
    final_price = fields["price_cents"] if "price_cents" in fields else highlight.price_cents
    final_promo = (
        fields["promo_price_cents"] if "promo_price_cents" in fields else highlight.promo_price_cents
    )
    _validate_prices(final_price, final_promo)

    if "title" in fields:
        fields["title"] = _clean_str(fields["title"], max_len=TITLE_MAX, field="title", required=True)
    if "description" in fields:
        fields["description"] = _clean_str(
            fields["description"], max_len=DESCRIPTION_MAX, field="description", required=False
        )
    if "photo_url" in fields:
        fields["photo_url"] = _clean_str(
            fields["photo_url"], max_len=PHOTO_URL_MAX, field="photo_url", required=False
        )
    if "product_url" in fields:
        fields["product_url"] = _validate_product_url(fields["product_url"])

    activating = fields.get("is_active") is True and not highlight.is_active
    if activating:
        _enforce_active_limit(db, highlight.tenant_id, exclude_id=highlight.id)

    for key, value in fields.items():
        setattr(highlight, key, value)
    highlight.updated_at = datetime.utcnow()
    db.add(highlight)
    db.flush()
    return highlight


def delete_highlight(db: Session, highlight: TenantProductHighlight) -> None:
    db.delete(highlight)
