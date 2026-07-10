from pydantic import BaseModel


class CancellationPolicyRuntime(BaseModel):
    """Mig 0107 — política de cancelamento do tenant, lida em runtime pelo app
    (substitui o hardcode CANCELLATION_FREE_HOURS=24/LATE_CANCELLATION_FEE_PERCENT=50
    de frontend/constants/cancellationPolicy.ts). Unidade canônica = minutos
    (mesma do banco); o app converte para horas na copy quando fizer sentido.
    """
    free_window_minutes: int
    late_fee_percent: float
    auto_refund_enabled: bool


class TenantFeatureRuntimeResponse(BaseModel):
    tenant_id: str
    features: dict[str, bool]
    cancellation_policy: CancellationPolicyRuntime
