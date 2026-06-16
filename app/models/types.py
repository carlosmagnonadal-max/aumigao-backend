"""Tipos de coluna de domínio.

Money: substitui Float nos campos monetários. No banco é NUMERIC(12,2) — preciso e
sem drift de ponto flutuante no armazenamento (relevante no Postgres de produção).
Arredonda para 2 casas (centavos) na ESCRITA, em qualquer dialeto (o round roda em
Python). Retorna float na LEITURA para preservar a aritmética float existente da base
(o caminho de split já arredonda em centavos — ver payment_split_service).
"""
from sqlalchemy import Numeric
from sqlalchemy.types import TypeDecorator


class Money(TypeDecorator):
    impl = Numeric(12, 2, asdecimal=False)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return round(float(value), 2)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return float(value)
