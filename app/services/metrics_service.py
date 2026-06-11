"""Serviço de agregação de métricas para o admin (Fase C).

Estratégia de série semanal: compatível com SQLite (testes) E Postgres (prod).
Ao invés de GROUP BY via SQL date_trunc (Postgres-only) ou strftime (SQLite-only),
fazemos a agregação em Python sobre os objetos já carregados — aceitável para as
janelas curtas de análise (8-12 semanas = <1000 registros tipicamente).

Formato da chave de semana: "YYYY-WXX" (ISO 8601 — isocalendar).
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.dependencies.tenant_scope import AdminTenantScope, apply_tenant_filter
from app.models.complaint import Complaint
from app.models.coupon import Coupon, CouponRedemption
from app.models.incentive_rule import IncentiveRule
from app.models.user import User
from app.models.walker_incentive import WalkerIncentive
from app.models.walker_referral import WalkerReferral

# Quantas semanas mostrar nas séries históricas
SERIES_WEEKS = 12


def _iso_week(dt: datetime | None) -> str | None:
    """Retorna 'YYYY-WXX' para um datetime (ou None se dt for None)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    iso = dt.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def _aggregate_by_week(
    items: list[Any],
    date_attr: str,
    count_attr: str | None = None,
    amount_attr: str | None = None,
) -> list[dict]:
    """Agrega uma lista de objetos por semana ISO.

    Retorna lista de dicts ordenados cronologicamente com chaves:
    - week (str)
    - count (int)
    - amount (float, apenas quando amount_attr fornecido)
    """
    week_counts: dict[str, int] = defaultdict(int)
    week_amounts: dict[str, float] = defaultdict(float)

    for item in items:
        dt = getattr(item, date_attr, None)
        week = _iso_week(dt)
        if week is None:
            continue
        week_counts[week] += 1
        if amount_attr is not None:
            val = getattr(item, amount_attr, None) or 0.0
            week_amounts[week] += float(val)

    sorted_weeks = sorted(week_counts.keys())
    # Limita às últimas SERIES_WEEKS semanas
    sorted_weeks = sorted_weeks[-SERIES_WEEKS:]

    if amount_attr is not None:
        return [
            {"week": w, "count": week_counts[w], "amount": round(week_amounts[w], 2)}
            for w in sorted_weeks
        ]
    return [{"week": w, "count": week_counts[w]} for w in sorted_weeks]


# ────────────────────────────────────────────────────────────────────────────
# 1. Cupons
# ────────────────────────────────────────────────────────────────────────────

def get_coupon_metrics(db: Session, scope: AdminTenantScope) -> dict:
    """Métricas de cupons, scoped por tenant."""
    # Query base de cupons
    coupon_q = apply_tenant_filter(db.query(Coupon), Coupon, scope)
    coupons = coupon_q.all()
    total_coupons = len(coupons)
    active_coupons = sum(1 for c in coupons if c.active)

    # Resgates: CouponRedemption também tem tenant_id
    redemption_q = apply_tenant_filter(
        db.query(CouponRedemption), CouponRedemption, scope
    )
    redemptions = redemption_q.all()
    total_redemptions = len(redemptions)
    total_discount_amount = round(
        sum(float(r.amount_discounted or 0.0) for r in redemptions), 2
    )

    # Top cupons por número de resgates
    coupon_id_to_code: dict[str, str] = {c.id: c.code for c in coupons}
    redemption_counts: dict[str, int] = defaultdict(int)
    for r in redemptions:
        redemption_counts[r.coupon_id] += 1

    top_coupons = sorted(
        redemption_counts.items(), key=lambda x: x[1], reverse=True
    )[:10]
    top_coupons_out = [
        {"code": coupon_id_to_code.get(cid, cid), "redemptions": cnt}
        for cid, cnt in top_coupons
    ]

    # Série semanal de resgates
    redemptions_by_week = _aggregate_by_week(
        redemptions, "created_at", amount_attr="amount_discounted"
    )

    return {
        "total_coupons": total_coupons,
        "active_coupons": active_coupons,
        "total_redemptions": total_redemptions,
        "total_discount_amount": total_discount_amount,
        "top_coupons": top_coupons_out,
        "redemptions_by_week": redemptions_by_week,
    }


# ────────────────────────────────────────────────────────────────────────────
# 2. Incentivos
# ────────────────────────────────────────────────────────────────────────────

def get_incentive_metrics(db: Session, scope: AdminTenantScope) -> dict:
    """Métricas de incentivos.

    IncentiveRule: scoped por tenant_id (coluna presente).
    WalkerIncentive: NÃO tem tenant_id; scoping via User.tenant_id
    (mesmo padrão do incentive_rule_service.list_granted).
    """
    # Regras
    rule_q = apply_tenant_filter(db.query(IncentiveRule), IncentiveRule, scope)
    rules = rule_q.all()
    total_rules = len(rules)
    active_rules = sum(1 for r in rules if r.active)

    # Concessões: scopar via walker_ids do tenant
    if scope.is_global:
        incentives = db.query(WalkerIncentive).all()
    else:
        walker_ids = [
            row[0]
            for row in db.query(User.id).filter(User.tenant_id == scope.tenant_id).all()
        ]
        if not walker_ids:
            incentives = []
        else:
            incentives = (
                db.query(WalkerIncentive)
                .filter(WalkerIncentive.walker_id.in_(walker_ids))
                .all()
            )

    total_granted = len(incentives)
    granted_amount = round(
        sum(float(i.amount or 0.0) for i in incentives), 2
    )

    # Quebra por tipo
    type_counts: dict[str, int] = defaultdict(int)
    type_amounts: dict[str, float] = defaultdict(float)
    for i in incentives:
        t = i.incentive_type or "unknown"
        type_counts[t] += 1
        type_amounts[t] += float(i.amount or 0.0)
    by_type = [
        {"incentive_type": t, "count": c, "amount": round(type_amounts[t], 2)}
        for t, c in sorted(type_counts.items())
    ]

    # Série semanal
    granted_by_week = _aggregate_by_week(incentives, "created_at")

    scope_note = (
        "IncentiveRule scoped por tenant; WalkerIncentive scoped via User.tenant_id"
    )

    return {
        "total_rules": total_rules,
        "active_rules": active_rules,
        "total_granted": total_granted,
        "granted_amount": granted_amount,
        "by_type": by_type,
        "granted_by_week": granted_by_week,
        "scope_note": scope_note,
    }


# ────────────────────────────────────────────────────────────────────────────
# 3. Indicações (WalkerReferral)
# ────────────────────────────────────────────────────────────────────────────

def get_referral_metrics(db: Session, scope: AdminTenantScope) -> dict:
    """Métricas de indicações de passeador.

    WalkerReferral NÃO possui tenant_id — passeadores são globais.
    Dados são globais independente do scope (admin ou super_admin).
    """
    referrals = db.query(WalkerReferral).all()
    total = len(referrals)

    # "Ativada" = status converted (passeador efetivamente onboardado)
    activated_count = sum(1 for r in referrals if r.status == "converted")

    # Recompensa liberada = reward_status == "paid" (soma de reward_amount)
    reward_released_amount = round(
        sum(
            float(r.reward_amount or 0.0)
            for r in referrals
            if r.reward_status == "paid"
        ),
        2,
    )

    # Quebra por status
    status_counts: dict[str, int] = defaultdict(int)
    for r in referrals:
        status_counts[r.status] += 1
    by_status = [
        {"status": s, "count": c}
        for s, c in sorted(status_counts.items())
    ]

    # Série semanal
    created_by_week = _aggregate_by_week(referrals, "created_at")

    scope_note = (
        "WalkerReferral não possui tenant_id; dados globais (todos os tenants)"
    )

    return {
        "total": total,
        "activated_count": activated_count,
        "reward_released_amount": reward_released_amount,
        "by_status": by_status,
        "created_by_week": created_by_week,
        "scope_note": scope_note,
    }


# ────────────────────────────────────────────────────────────────────────────
# 4. Ocorrências (Complaint)
# ────────────────────────────────────────────────────────────────────────────

def get_complaint_metrics(db: Session, scope: AdminTenantScope) -> dict:
    """Métricas de ocorrências, scoped por tenant.

    Complaint.tenant_id é nullable=True — apply_tenant_filter funciona, mas
    para super_admin retorna todos (is_global=True).

    avg_resolution_hours: calculado em Python (resolved_at - created_at).
    Retorna null se não houver nenhuma ocorrência com resolved_at preenchido.
    """
    complaint_q = apply_tenant_filter(db.query(Complaint), Complaint, scope)
    complaints = complaint_q.all()
    total = len(complaints)

    open_count = sum(
        1 for c in complaints if c.status not in {"resolvida", "rejeitada"}
    )
    resolved_count = sum(
        1 for c in complaints if c.status in {"resolvida", "rejeitada"}
    )

    # avg_resolution_hours
    resolution_durations: list[float] = []
    for c in complaints:
        if c.resolved_at and c.created_at:
            resolved_at = c.resolved_at
            created_at = c.created_at
            # normaliza tz
            if resolved_at.tzinfo is None:
                resolved_at = resolved_at.replace(tzinfo=timezone.utc)
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            diff = (resolved_at - created_at).total_seconds()
            if diff >= 0:
                resolution_durations.append(diff / 3600.0)

    avg_resolution_hours: float | None = None
    if resolution_durations:
        avg_resolution_hours = round(
            sum(resolution_durations) / len(resolution_durations), 2
        )

    # Quebra por categoria
    category_counts: dict[str, int] = defaultdict(int)
    for c in complaints:
        category_counts[c.category] += 1
    by_category = [
        {"category": cat, "count": cnt}
        for cat, cnt in sorted(category_counts.items())
    ]

    # Quebra por severidade
    severity_counts: dict[str, int] = defaultdict(int)
    for c in complaints:
        severity_counts[c.severity] += 1
    by_severity = [
        {"severity": sev, "count": cnt}
        for sev, cnt in sorted(severity_counts.items())
    ]

    # Série semanal (por data de abertura)
    opened_by_week = _aggregate_by_week(complaints, "created_at")

    return {
        "total": total,
        "open_count": open_count,
        "resolved_count": resolved_count,
        "avg_resolution_hours": avg_resolution_hours,
        "by_category": by_category,
        "by_severity": by_severity,
        "opened_by_week": opened_by_week,
    }
