"""Schemas Pydantic para os endpoints de métricas do admin (Fase C).

Cada schema descreve o JSON de resposta exato de um endpoint GET .../metrics.
Séries semanais: lista de objetos {week: str (ISO "YYYY-WXX"), count: int, ...}
"""
from __future__ import annotations

from pydantic import BaseModel


# ────────────────────────────────────────────────────────────────────────────
# Ponto compartilhado: item de série semanal
# ────────────────────────────────────────────────────────────────────────────

class WeekCount(BaseModel):
    """Um ponto de série temporal agrupado por semana ISO."""
    week: str       # "YYYY-WXX"  ex: "2026-W23"
    count: int


class WeekAmount(BaseModel):
    """Semana + valor monetário (ex: soma de descontos)."""
    week: str       # "YYYY-WXX"
    count: int
    amount: float


# ────────────────────────────────────────────────────────────────────────────
# 1. GET /admin/coupons/metrics
# ────────────────────────────────────────────────────────────────────────────

class TopCoupon(BaseModel):
    """Cupom com maior número de resgates."""
    code: str
    redemptions: int


class CouponMetricsResponse(BaseModel):
    """
    JSON shape:
    {
        "total_coupons": 12,
        "active_coupons": 8,
        "total_redemptions": 47,
        "total_discount_amount": 325.50,
        "top_coupons": [
            {"code": "BEMVINDO10", "redemptions": 15},
            ...
        ],
        "redemptions_by_week": [
            {"week": "2026-W20", "count": 4, "amount": 28.00},
            ...
        ]
    }
    """
    total_coupons: int
    active_coupons: int
    total_redemptions: int
    total_discount_amount: float
    top_coupons: list[TopCoupon]
    redemptions_by_week: list[WeekAmount]


# ────────────────────────────────────────────────────────────────────────────
# 2. GET /admin/incentives/metrics
# ────────────────────────────────────────────────────────────────────────────

class IncentiveByType(BaseModel):
    """Quebra de incentivos concedidos por tipo."""
    incentive_type: str
    count: int
    amount: float


class IncentiveMetricsResponse(BaseModel):
    """
    JSON shape:
    {
        "total_rules": 5,
        "active_rules": 3,
        "total_granted": 120,
        "granted_amount": 980.00,
        "by_type": [
            {"incentive_type": "recognition", "count": 80, "amount": 0.0},
            {"incentive_type": "monetary", "count": 40, "amount": 980.0}
        ],
        "granted_by_week": [
            {"week": "2026-W20", "count": 8},
            ...
        ],
        "scope_note": "IncentiveRule scoped por tenant; WalkerIncentive scoped via User.tenant_id"
    }
    """
    total_rules: int
    active_rules: int
    total_granted: int
    granted_amount: float
    by_type: list[IncentiveByType]
    granted_by_week: list[WeekCount]
    scope_note: str


# ────────────────────────────────────────────────────────────────────────────
# 3. GET /admin/referrals/metrics
# ────────────────────────────────────────────────────────────────────────────

class ReferralByStatus(BaseModel):
    """Quebra de indicações por status."""
    status: str
    count: int


class ReferralMetricsResponse(BaseModel):
    """
    JSON shape:
    {
        "total": 85,
        "activated_count": 30,
        "reward_released_amount": 1500.00,
        "by_status": [
            {"status": "pending", "count": 20},
            {"status": "approved", "count": 35},
            {"status": "converted", "count": 30}
        ],
        "created_by_week": [
            {"week": "2026-W20", "count": 6},
            ...
        ],
        "scope_note": "WalkerReferral não possui tenant_id; dados globais (todos os tenants)"
    }
    """
    total: int
    activated_count: int
    reward_released_amount: float
    by_status: list[ReferralByStatus]
    created_by_week: list[WeekCount]
    scope_note: str


# ────────────────────────────────────────────────────────────────────────────
# 4. GET /admin/complaints/metrics
# ────────────────────────────────────────────────────────────────────────────

class ComplaintByCategory(BaseModel):
    category: str
    count: int


class ComplaintBySeverity(BaseModel):
    severity: str
    count: int


class ComplaintMetricsResponse(BaseModel):
    """
    JSON shape:
    {
        "total": 200,
        "open_count": 45,
        "resolved_count": 120,
        "avg_resolution_hours": 18.5,
        "by_category": [
            {"category": "comportamento", "count": 60},
            ...
        ],
        "by_severity": [
            {"severity": "baixa", "count": 100},
            {"severity": "media", "count": 60},
            {"severity": "alta", "count": 30},
            {"severity": "critica", "count": 10}
        ],
        "opened_by_week": [
            {"week": "2026-W20", "count": 12},
            ...
        ]
    }

    Nota: avg_resolution_hours é null quando não há ocorrências resolvidas com
    resolved_at preenchido.
    """
    total: int
    open_count: int
    resolved_count: int
    avg_resolution_hours: float | None
    by_category: list[ComplaintByCategory]
    by_severity: list[ComplaintBySeverity]
    opened_by_week: list[WeekCount]
