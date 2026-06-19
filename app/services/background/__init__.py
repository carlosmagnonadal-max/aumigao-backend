"""Background check provider abstraction — Fase 0.

Pacote plugavel: cada tenant pode escolher um provedor de background check.
Unico provedor FUNCIONAL agora: "manual" (fluxo de certidoes offline + validacao admin).
Slots pagos (flagcheck/idwall/serpro) reservados para integracao futura.

Uso:
    from app.services.background.registry import get_background_provider
    provider = get_background_provider(db, tenant_id)
    status = provider.get_background_status(profile, certificates)
"""
from app.services.background.registry import get_background_provider

__all__ = ["get_background_provider"]
