"""Invariante de precisão do split em Decimal (refactor Float -> Decimal/Numeric).

Garante que a divisão de valores "difíceis" (que expõem erro de ponto flutuante)
reconcilia EXATAMENTE com Decimal: platform + tenant + walker == amount, sem o
resíduo de centavo que o float acumula. Também confere a borda (retorno float, JSON
number) e que os valores devolvidos têm no máximo 2 casas decimais.
"""
from decimal import Decimal

import pytest

from app.core.money import q2, q4, to_money
from app.services.payment_split_service import compute_split, walker_percent_from_split

# Valores "difíceis" citados no refactor + dízimas e centavos quebrados.
HARD_AMOUNTS = [0.01, 33.33, 129.99, 10.05, 0.10, 99.99, 1234.56, 49.90, 0.03]
COMMISSIONS = [0.0, 5.0, 8.0, 10.0, 12.0, 18.0, 33.33]
MARGINS = [0.0, 5.0, 10.0]


def test_split_reconciles_exactly_in_decimal():
    """A soma das 3 fatias, em Decimal, é EXATAMENTE igual ao amount (tolerância 0)."""
    for a in HARD_AMOUNTS:
        for c in COMMISSIONS:
            for m in MARGINS:
                s = compute_split(a, c, m)
                soma = (
                    to_money(s["platform_amount"])
                    + to_money(s["tenant_amount"])
                    + to_money(s["walker_amount"])
                )
                assert soma == q2(a), (a, c, m, s, soma)


def test_split_parts_have_at_most_two_decimals():
    for a in HARD_AMOUNTS:
        for c in COMMISSIONS:
            s = compute_split(a, c, 5.0)
            for key in ("platform_amount", "tenant_amount", "walker_amount"):
                d = to_money(s[key])
                assert d == d.quantize(Decimal("0.01")), (a, c, key, d)


def test_split_returns_native_float_for_api_contract():
    """A borda entrega float nativo (Pydantic serializa como número JSON)."""
    s = compute_split(129.99, 12.0, 5.0)
    for key in ("platform_amount", "tenant_amount", "walker_amount", "commission_percent"):
        assert isinstance(s[key], float), (key, type(s[key]))


def test_walker_amount_is_residual_no_lost_cent():
    """walker_amount = amount - platform - tenant, sem centavo perdido/criado."""
    for a in HARD_AMOUNTS:
        s = compute_split(a, 33.33, 10.0)
        expected = q2(to_money(a) - to_money(s["platform_amount"]) - to_money(s["tenant_amount"]))
        assert to_money(s["walker_amount"]) == expected, (a, s)


def test_gateway_percent_reconciles_within_one_cent():
    for a in HARD_AMOUNTS:
        if a <= 0:
            continue
        s = compute_split(a, 12.0, 5.0)
        pct = walker_percent_from_split(s)
        gateway_walker = q2(to_money(a) * to_money(pct) / Decimal("100"))
        assert abs(gateway_walker - to_money(s["walker_amount"])) <= Decimal("0.01"), (a, pct, s)


def test_to_money_never_reintroduces_float_error():
    """Decimal(str(x)) evita o ruído binário de Decimal(float) direto."""
    assert to_money(0.1) == Decimal("0.1")
    assert q2(0.1 + 0.2) == Decimal("0.30")  # o clássico 0.30000000000000004
    assert q4(1.0 / 3.0) == Decimal("0.3333")


@pytest.mark.parametrize("bad", [-5.0, 150.0])
def test_commission_is_clamped(bad):
    s = compute_split(100.0, bad, 0.0)
    assert 0.0 <= s["commission_percent"] <= 100.0
