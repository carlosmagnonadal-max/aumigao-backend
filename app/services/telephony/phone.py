"""Normalizador E.164 para telefones brasileiros (fixo 10 dígitos / celular 11)."""
from __future__ import annotations

from app.utils.registration_validation import only_digits, validate_brazilian_phone


def to_e164_br(raw: str | None) -> str | None:
    """Converte um telefone BR em formato livre para '+55DDDNUMERO'.

    Aceita máscara, prefixo internacional (+55 / 0055) e zero de tronco (0DDD...).
    Devolve None quando o número não é um telefone BR válido (0800, curto, repetido).
    """
    digits = only_digits(raw)
    if not digits:
        return None
    if digits.startswith("00"):
        digits = digits[2:]
    if digits.startswith("55") and len(digits) in (12, 13):
        digits = digits[2:]
    elif digits.startswith("0") and len(digits) in (11, 12):
        digits = digits[1:]
    if not validate_brazilian_phone(digits):
        return None
    return f"+55{digits}"
