import re

CPF_ERROR = "Informe um CPF válido."
PHONE_ERROR = "Informe um telefone válido com DDD."
EMAIL_ERROR = "Informe um e-mail válido."


def only_digits(value: str | None) -> str:
    return re.sub(r"\D", "", value or "")


def normalize_email(value: str | None) -> str:
    return (value or "").strip().lower()


def validate_cpf(value: str | None) -> bool:
    cpf = only_digits(value)
    if len(cpf) != 11:
        return False
    if cpf == cpf[0] * 11:
        return False

    def digit(base: str, factor: int) -> int:
        total = 0
        for char in base:
            total += int(char) * factor
            factor -= 1
        remainder = (total * 10) % 11
        return 0 if remainder == 10 else remainder

    return digit(cpf[:9], 10) == int(cpf[9]) and digit(cpf[:10], 11) == int(cpf[10])


def validate_brazilian_phone(value: str | None) -> bool:
    phone = only_digits(value)
    if len(phone) not in {10, 11}:
        return False
    if phone == phone[0] * len(phone):
        return False
    ddd = int(phone[:2])
    if ddd < 11 or ddd > 99:
        return False
    if len(phone) == 11:
        return phone[2] == "9"
    return phone[2] in {"2", "3", "4", "5"}


def validate_email(value: str | None) -> bool:
    email = normalize_email(value)
    if not email or re.search(r"\s", email):
        return False
    return bool(re.fullmatch(r"[^@\s]+@[^@\s.][^@\s]*\.[^@\s.]{2,}", email)) and not email.endswith(".")


def normalize_cpf_or_raise(value: str | None) -> str:
    cpf = only_digits(value)
    if not validate_cpf(cpf):
        raise ValueError(CPF_ERROR)
    return cpf


def normalize_phone_or_raise(value: str | None) -> str:
    phone = only_digits(value)
    if not validate_brazilian_phone(phone):
        raise ValueError(PHONE_ERROR)
    return phone


def normalize_email_or_raise(value: str | None) -> str:
    email = normalize_email(value)
    if not validate_email(email):
        raise ValueError(EMAIL_ERROR)
    return email
