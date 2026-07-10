"""Motor dos alertas de custo (fase 1: tenant / commission_entries).

Lógica pura (period_window, forecast_amount, crossed_thresholds) separada do
acesso a banco (tenant_spend, evaluate_cost_alerts — próximo lote) para ser
testável sem DB. Dinheiro = Decimal fim-a-fim; períodos em fuso LOCAL do tenant
(mesma disciplina de app/lib/walk_time.py).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo

LOGGER = logging.getLogger("aumigao.cost_alerts")

_CENT = Decimal("0.01")
# Fração mínima do período decorrida para projetar (guarda contra divisão
# por ~zero e projeção absurda no início do período).
MIN_FORECAST_FRACTION = 0.10
DEFAULT_TZ = "America/Bahia"


def period_window(period: str, now_utc: datetime, tz_name: str | None) -> tuple[datetime, datetime, str, float]:
    """(start_utc, end_utc, period_key, elapsed_fraction) da janela que contém
    now_utc, calculada no fuso local do tenant. Datetimes retornam UTC naive
    (padrão do projeto). Semana = ISO (segunda a domingo)."""
    tz = ZoneInfo(tz_name or DEFAULT_TZ)
    local_now = now_utc.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz)

    if period == "daily":
        local_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        local_end = local_start + timedelta(days=1)
        key = local_start.strftime("%Y-%m-%d")
    elif period == "weekly":
        day_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        local_start = day_start - timedelta(days=local_now.weekday())
        local_end = local_start + timedelta(days=7)
        iso = local_start.isocalendar()
        key = f"{iso.year}-W{iso.week:02d}"
    elif period == "monthly":
        local_start = local_now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if local_start.month == 12:
            local_end = local_start.replace(year=local_start.year + 1, month=1)
        else:
            local_end = local_start.replace(month=local_start.month + 1)
        key = local_start.strftime("%Y-%m")
    else:
        raise ValueError(f"Período inválido: {period}")

    start_utc = local_start.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    end_utc = local_end.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    now_naive = now_utc.replace(tzinfo=None)
    total = (end_utc - start_utc).total_seconds()
    elapsed = max(0.0, min(1.0, (now_naive - start_utc).total_seconds() / total))
    return start_utc, end_utc, key, elapsed


def forecast_amount(spend: Decimal, elapsed_fraction: float) -> Decimal | None:
    """Projeção linear do gasto até o fim do período. None se o período mal
    começou (fração < MIN_FORECAST_FRACTION) — projetar cedo demais gera alarme falso."""
    if elapsed_fraction < MIN_FORECAST_FRACTION:
        return None
    return (spend / Decimal(str(elapsed_fraction))).quantize(_CENT, rounding=ROUND_HALF_UP)


def crossed_thresholds(
    *,
    spend: Decimal,
    budget: Decimal,
    thresholds: list[int],
    evaluation: str,
    elapsed_fraction: float,
) -> list[tuple[int, str]]:
    """Thresholds cruzados agora, como (threshold, kind). Regras:
    - actual: spend >= budget * T/100.
    - forecast: só T >= 100 (projeção interessa pra "vai estourar") e
      projeção >= budget * T/100.
    O dedupe de já-notificado NÃO é aqui — é o índice único de cost_alert_events."""
    hits: list[tuple[int, str]] = []
    projected = forecast_amount(spend, elapsed_fraction)
    for threshold in sorted(thresholds):
        limit = (budget * Decimal(threshold) / Decimal(100)).quantize(_CENT, rounding=ROUND_HALF_UP)
        if evaluation in ("actual", "both") and spend >= limit and spend > 0:
            hits.append((threshold, "actual"))
        if (
            evaluation in ("forecast", "both")
            and threshold >= 100
            and projected is not None
            and projected >= limit
            and spend > 0
        ):
            hits.append((threshold, "forecast"))
    return hits
