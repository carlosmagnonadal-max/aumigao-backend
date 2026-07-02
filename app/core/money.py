"""Helpers de precisão monetária (Decimal).

Dinheiro é armazenado como NUMERIC no banco e calculado em Decimal em Python —
nunca em float — para eliminar o drift de ponto flutuante acumulável.

Regras de ouro:
  - Converter QUALQUER entrada externa via `to_money` (usa str() internamente).
    NUNCA `Decimal(x)` direto de um float — isso reintroduz o erro binário
    (ex.: Decimal(0.1) == 0.1000000000000000055...).
  - Arredondar em centavos com ROUND_HALF_UP (regra "meio pra cima" — coerente
    com faturamento no Brasil), via `q2`. Para valores unitários com 4 casas
    (ex.: unit_value do crédito), usar `q4`.
  - A fronteira da API continua entregando float/JSON number: usar `to_float`
    só na borda de saída quando o schema exige float. O Pydantic v2 serializa
    Decimal como número JSON nativamente, então na maioria dos casos nem é preciso.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

CENT = Decimal("0.01")
UNIT4 = Decimal("0.0001")
ZERO = Decimal("0.00")


def to_money(value) -> Decimal:
    """Converte um valor arbitrário (int/float/str/Decimal/None) em Decimal seguro.

    None → Decimal('0'). Floats passam por str() para evitar o erro binário.
    """
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        # str(float) dá a representação decimal mais curta que round-trips —
        # evita o ruído de Decimal(float) direto.
        return Decimal(str(value))
    return Decimal(str(value))


def q2(value) -> Decimal:
    """Quantiza para 2 casas (centavos) com ROUND_HALF_UP."""
    return to_money(value).quantize(CENT, rounding=ROUND_HALF_UP)


def q4(value) -> Decimal:
    """Quantiza para 4 casas (valor unitário) com ROUND_HALF_UP."""
    return to_money(value).quantize(UNIT4, rounding=ROUND_HALF_UP)


def to_float(value) -> float:
    """Borda de saída: Decimal → float. Usar apenas onde o contrato exige float."""
    if value is None:
        return 0.0
    return float(value)
