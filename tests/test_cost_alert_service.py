"""Lógica pura dos alertas de custo: janela de período, forecast, thresholds."""
from datetime import datetime
from decimal import Decimal

from app.services.cost_alert_service import (
    crossed_thresholds,
    forecast_amount,
    period_window,
)

TZ = "America/Bahia"  # UTC-3 sem DST


class TestPeriodWindow:
    def test_monthly_window_local_tz(self):
        # 2026-07-10 00:30 UTC = 2026-07-09 21:30 local → período é JULHO local
        now = datetime(2026, 7, 10, 0, 30)
        start, end, key, frac = period_window("monthly", now, TZ)
        assert key == "2026-07"
        assert start == datetime(2026, 7, 1, 3, 0)   # 01/07 00:00 local em UTC
        assert end == datetime(2026, 8, 1, 3, 0)
        assert 0.0 < frac < 1.0

    def test_daily_key_uses_local_date_not_utc(self):
        now = datetime(2026, 7, 10, 1, 0)  # 09/07 22:00 local
        _, _, key, _ = period_window("daily", now, TZ)
        assert key == "2026-07-09"

    def test_weekly_iso_key(self):
        now = datetime(2026, 7, 10, 12, 0)  # sexta 10/07 local
        start, end, key, _ = period_window("weekly", now, TZ)
        assert key == "2026-W28"
        assert start == datetime(2026, 7, 6, 3, 0)   # segunda 06/07 00:00 local
        assert end == datetime(2026, 7, 13, 3, 0)

    def test_fraction_bounds(self):
        start_of_month = datetime(2026, 7, 1, 3, 0, 1)  # 1s após virada local
        _, _, _, frac = period_window("monthly", start_of_month, TZ)
        assert 0.0 < frac < 0.001


class TestForecast:
    def test_linear_projection(self):
        assert forecast_amount(Decimal("100"), 0.5) == Decimal("200")

    def test_none_when_period_just_started(self):
        assert forecast_amount(Decimal("100"), 0.05) is None
        assert forecast_amount(Decimal("100"), 0.0) is None

    def test_decimal_no_float(self):
        result = forecast_amount(Decimal("33.33"), 0.3333)
        assert isinstance(result, Decimal)


class TestCrossedThresholds:
    def test_actual_crossing(self):
        hits = crossed_thresholds(
            spend=Decimal("400"), budget=Decimal("500"),
            thresholds=[50, 80, 100], evaluation="actual", elapsed_fraction=0.5,
        )
        assert hits == [(50, "actual"), (80, "actual")]

    def test_forecast_only_at_or_above_100(self):
        # gasto 300 em 50% do período → forecast 600 ≥ budget 500 → dispara 100/forecast
        hits = crossed_thresholds(
            spend=Decimal("300"), budget=Decimal("500"),
            thresholds=[50, 80, 100], evaluation="forecast", elapsed_fraction=0.5,
        )
        assert hits == [(100, "forecast")]

    def test_both_combines_kinds(self):
        hits = crossed_thresholds(
            spend=Decimal("300"), budget=Decimal("500"),
            thresholds=[50, 100], evaluation="both", elapsed_fraction=0.5,
        )
        assert (50, "actual") in hits and (100, "forecast") in hits

    def test_no_forecast_when_period_just_started(self):
        hits = crossed_thresholds(
            spend=Decimal("300"), budget=Decimal("500"),
            thresholds=[100], evaluation="forecast", elapsed_fraction=0.02,
        )
        assert hits == []

    def test_zero_budget_never_divides(self):
        hits = crossed_thresholds(
            spend=Decimal("0"), budget=Decimal("500"),
            thresholds=[50], evaluation="both", elapsed_fraction=0.5,
        )
        assert hits == []
