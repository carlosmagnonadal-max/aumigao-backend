"""Tipos de coluna de domínio.

Money: substitui Float nos campos monetários. No banco é NUMERIC(12,2) — preciso e
sem drift de ponto flutuante no armazenamento (relevante no Postgres de produção).
Arredonda para 2 casas (centavos) na ESCRITA, em qualquer dialeto (o round roda em
Python via Decimal/ROUND_HALF_UP). Retorna float na LEITURA para preservar o
contrato JSON em reais (os response schemas tipam float) e a aritmética float
existente da base — os caminhos de cálculo de dinheiro já operam em Decimal
internamente (ver app.core.money e payment_split_service) e só entregam float na
borda.

Money4: variante NUMERIC(12,4) para VALORES UNITÁRIOS que legitimamente usam 4
casas (ex.: unit_value do crédito = price / walks_per_cycle).
"""
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import Numeric
from sqlalchemy.types import TypeDecorator

_CENT = Decimal("0.01")
_UNIT4 = Decimal("0.0001")


class Money(TypeDecorator):
    impl = Numeric(12, 2, asdecimal=False)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        # Quantiza em centavos com ROUND_HALF_UP via Decimal (str() evita o erro
        # binário de Decimal(float) direto).
        return Decimal(str(value)).quantize(_CENT, rounding=ROUND_HALF_UP)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return float(value)


class Money4(TypeDecorator):
    impl = Numeric(12, 4, asdecimal=False)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return Decimal(str(value)).quantize(_UNIT4, rounding=ROUND_HALF_UP)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return float(value)
