"""FIX 4 (P1) — DEFAULT_COMMISSION_PERCENT legado 20.0 + fallback silencioso 10%.

- DEFAULT_COMMISSION_PERCENT foi neutralizado (20.0 -> 10.0, piso Pro).
- commission_default_for_plan: plano DESCONHECIDO (string não vazia fora da tabela)
  não cobra às cegas -> 0% (com log de erro). Plano AUSENTE (None/"") mantém 10%.
"""
import importlib

import app.models.tenant_payment_config as tpc


def test_default_commission_percent_is_pro_floor_not_20():
    assert tpc.DEFAULT_COMMISSION_PERCENT == 10.0
    # A coluna usa esse default (novos registros sem plano resolvido não cobram 20%).
    assert tpc.TenantPaymentConfig.__table__.c.commission_percent.default.arg == 10.0


def test_unknown_plan_v1_returns_zero_not_ten(monkeypatch):
    monkeypatch.setattr(tpc, "_PRICING_V2_ENABLED", False)
    assert tpc.commission_default_for_plan("plano-que-nao-existe") == 0.0
    # conhecidos continuam corretos
    assert tpc.commission_default_for_plan("starter") == 12.0
    # ausente mantém piso
    assert tpc.commission_default_for_plan(None) == 10.0


def test_unknown_plan_v2_returns_zero_not_ten(monkeypatch):
    monkeypatch.setattr(tpc, "_PRICING_V2_ENABLED", True)
    assert tpc.commission_default_for_plan("plano-que-nao-existe") == 0.0
    # conhecidos v2 / legado mapeado
    assert tpc.commission_default_for_plan("pro") == 10.0
    assert tpc.commission_default_for_plan("enterprise") == 5.0
    assert tpc.commission_default_for_plan("business") == 10.0  # legado -> pro
    # ausente -> piso Pro
    assert tpc.commission_default_for_plan(None) == 10.0


def test_unknown_plan_logs_error(caplog):
    import logging
    with caplog.at_level(logging.ERROR):
        tpc.commission_default_for_plan("gibberish-plan")
    assert any("DESCONHECIDO" in r.message for r in caplog.records)
