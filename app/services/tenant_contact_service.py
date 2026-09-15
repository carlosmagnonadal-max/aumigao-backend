"""tenant_contact_service — telefone de contato/suporte do estabelecimento (tenant).

Extraído de `app.services.walk_emergency_service._tenant_support_phone` (S1,
Botão de Emergência) para reuso no S3 (`establishment_support_phone` nos
payloads do passeador). Mesma regra, mesmo comportamento: NÃO é dado pessoal
do tutor — é o telefone público de contato/suporte do tenant.

Fonte: TenantSettings.support_phone; fallback Tenant.contact_phone.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.tenant import Tenant, TenantSettings
from app.services.telephony import to_e164_br


def resolve_tenant_support_phone(db: Session, tenant_id: str | None) -> str | None:
    if not tenant_id:
        return None
    settings = db.query(TenantSettings).filter(TenantSettings.tenant_id == tenant_id).first()
    raw = (settings.support_phone if settings else None) or ""
    if not raw.strip():
        tenant = db.get(Tenant, tenant_id)
        raw = (tenant.contact_phone if tenant else None) or ""
    raw = raw.strip()
    if not raw:
        return None
    return to_e164_br(raw) or raw
