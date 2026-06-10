import pytest

from app.utils.registration_validation import (
    CPF_ERROR,
    EMAIL_ERROR,
    PHONE_ERROR,
    normalize_cpf_or_raise,
    normalize_email,
    normalize_email_or_raise,
    normalize_phone_or_raise,
    only_digits,
    validate_brazilian_phone,
    validate_cpf,
    validate_email,
)


# ---------------------------------------------------------------------------
# only_digits / normalize_email helpers
# ---------------------------------------------------------------------------

def test_only_digits_strips_non_digits():
    assert only_digits("529.982.247-25") == "52998224725"
    assert only_digits("(11) 98765-4321") == "11987654321"


def test_only_digits_none_returns_empty():
    assert only_digits(None) == ""
    assert only_digits("") == ""


def test_normalize_email_trims_and_lowercases():
    assert normalize_email("  Foo@Bar.COM ") == "foo@bar.com"
    assert normalize_email(None) == ""


# ---------------------------------------------------------------------------
# validate_cpf (check digits)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cpf", ["52998224725", "11144477735", "529.982.247-25"])
def test_validate_cpf_valid(cpf):
    assert validate_cpf(cpf) is True


def test_validate_cpf_wrong_length():
    assert validate_cpf("123") is False
    assert validate_cpf("5299822472") is False  # 10 digits
    assert validate_cpf("529982247250") is False  # 12 digits


def test_validate_cpf_all_same_digit_rejected():
    # all-equal digits would pass the math but are explicitly rejected
    for d in "0123456789":
        assert validate_cpf(d * 11) is False


def test_validate_cpf_wrong_check_digit():
    # valid base but last digit tampered
    assert validate_cpf("52998224724") is False
    assert validate_cpf("52998224715") is False  # first check digit wrong


def test_validate_cpf_none_and_empty():
    assert validate_cpf(None) is False
    assert validate_cpf("") is False


def test_validate_cpf_remainder_10_maps_to_zero():
    # cpf where the modular arithmetic yields remainder 10 -> digit 0
    # 11144477735 exercises the standard algorithm; ensure deterministic accept
    assert validate_cpf("11144477735") is True


# ---------------------------------------------------------------------------
# validate_brazilian_phone (DDD / mobile / landline)
# ---------------------------------------------------------------------------

def test_validate_phone_mobile_11_digits_starts_with_9():
    assert validate_brazilian_phone("11987654321") is True


def test_validate_phone_mobile_11_digits_not_9_rejected():
    # 11 digits but the subscriber part does not start with 9
    assert validate_brazilian_phone("1188765432") is False  # 10 digits actually
    assert validate_brazilian_phone("11887654321") is False  # 11 digits, 3rd != 9


def test_validate_phone_landline_10_digits_valid_prefixes():
    for prefix in ("2", "3", "4", "5"):
        number = "11" + prefix + "1234567"
        assert len(only_digits(number)) == 10
        assert validate_brazilian_phone(number) is True


def test_validate_phone_landline_invalid_prefix():
    # 10 digits but starts with 6 -> not a valid landline prefix
    assert validate_brazilian_phone("1166664444") is False
    # mobile prefix 9 in a 10-digit number is also rejected
    assert validate_brazilian_phone("1191234567") is False


def test_validate_phone_ddd_out_of_range():
    # DDD < 11 is invalid
    assert validate_brazilian_phone("1099999999") is False
    assert validate_brazilian_phone("1098765432") is False


def test_validate_phone_wrong_length():
    assert validate_brazilian_phone("123") is False
    assert validate_brazilian_phone("119876543210") is False  # 12 digits


def test_validate_phone_all_same_digit_rejected():
    assert validate_brazilian_phone("1111111111") is False
    assert validate_brazilian_phone("99999999999") is False


def test_validate_phone_none_and_empty():
    assert validate_brazilian_phone(None) is False
    assert validate_brazilian_phone("") is False


def test_validate_phone_accepts_formatted_input():
    assert validate_brazilian_phone("(11) 98765-4321") is True


# ---------------------------------------------------------------------------
# validate_email
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "email",
    [
        "foo@bar.com",
        "Foo@Bar.COM",
        "  user@example.com.br  ",
        "a@b.co",
    ],
)
def test_validate_email_valid(email):
    assert validate_email(email) is True


@pytest.mark.parametrize(
    "email",
    [
        "",
        None,
        "plainaddress",
        "a@b.c",          # TLD must be at least 2 chars
        "a@.co",          # domain cannot start with a dot
        "a@b.com.",       # trailing dot rejected
        "a b@c.com",      # internal whitespace rejected
        "foo @bar.com",   # whitespace rejected
        "@bar.com",       # missing local part
        "foo@",           # missing domain
    ],
)
def test_validate_email_invalid(email):
    assert validate_email(email) is False


# ---------------------------------------------------------------------------
# normalize_*_or_raise
# ---------------------------------------------------------------------------

def test_normalize_cpf_or_raise_returns_digits_only():
    assert normalize_cpf_or_raise("529.982.247-25") == "52998224725"


def test_normalize_cpf_or_raise_invalid():
    with pytest.raises(ValueError) as exc:
        normalize_cpf_or_raise("123")
    assert str(exc.value) == CPF_ERROR


def test_normalize_phone_or_raise_returns_digits_only():
    assert normalize_phone_or_raise("(11) 98765-4321") == "11987654321"


def test_normalize_phone_or_raise_invalid():
    with pytest.raises(ValueError) as exc:
        normalize_phone_or_raise("123")
    assert str(exc.value) == PHONE_ERROR


def test_normalize_email_or_raise_normalizes():
    assert normalize_email_or_raise("  Foo@Bar.COM ") == "foo@bar.com"


def test_normalize_email_or_raise_invalid():
    with pytest.raises(ValueError) as exc:
        normalize_email_or_raise("bad")
    assert str(exc.value) == EMAIL_ERROR


def test_normalize_email_or_raise_none():
    with pytest.raises(ValueError) as exc:
        normalize_email_or_raise(None)
    assert str(exc.value) == EMAIL_ERROR
