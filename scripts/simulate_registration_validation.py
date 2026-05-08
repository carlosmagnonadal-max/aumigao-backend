import os
from datetime import datetime

os.environ["EXPO_PUBLIC_DEMO_MODE"] = "false"
os.environ["DEMO_MODE"] = "false"

from fastapi.testclient import TestClient

from app.main import app


VALID_CPF = "52998224725"
VALID_PHONE = "71999999999"
VALID_LANDLINE = "7133334444"
VALID_EMAIL = "usuario.teste@gmail.com"
WALKER_COMPLETE_FIELDS = {
    "profile_photo_url": "beta://profile-photo",
    "document_url": "beta://identity-front",
    "identity_document_back_url": "beta://identity-back",
    "proof_of_address_url": "beta://proof-of-address",
    "bio": "Sou apaixonado por pets, tenho rotina organizada, cuido com carinho e mantenho tutores informados durante todo o passeio.",
}


def unique_email(prefix: str) -> str:
    stamp = datetime.utcnow().strftime("%Y%m%d%H%M%S%f")
    return f"{prefix}-{stamp}@gmail.com"


def unique_cpf() -> str:
    base = datetime.utcnow().strftime("%H%M%S%f")[-9:]

    def digit(numbers: str) -> str:
        factor = len(numbers) + 1
        total = sum(int(number) * (factor - index) for index, number in enumerate(numbers))
        value = (total * 10) % 11
        return "0" if value == 10 else str(value)

    first = digit(base)
    return base + first + digit(base + first)


def unique_mobile_phone() -> str:
    return "719" + datetime.utcnow().strftime("%H%M%S%f")[-8:]


def unique_landline_phone() -> str:
    return "713" + datetime.utcnow().strftime("%H%M%S%f")[-7:]


def expect_bad(response, message: str):
    assert response.status_code == 400, response.text
    assert response.json()["detail"] == message, response.text


def main():
    client = TestClient(app)

    for cpf in ["12345678900", "11111111111", "123", "123456789012"]:
        response = client.post(
            "/api/partner-applications",
            json={
                "full_name": "Passeador Validacao",
                "cpf": cpf,
                "phone": VALID_PHONE,
                "email": unique_email("walker-cpf-invalid"),
                "accepted_declaration": True,
                **WALKER_COMPLETE_FIELDS,
            },
        )
        expect_bad(response, "Informe um CPF válido.")

    for phone in ["999", "11111111111", "719999999999999"]:
        response = client.post(
            "/api/partner-applications",
            json={
                "full_name": "Passeador Validacao",
                "cpf": unique_cpf(),
                "phone": phone,
                "email": unique_email("walker-phone-invalid"),
                "accepted_declaration": True,
                **WALKER_COMPLETE_FIELDS,
            },
        )
        expect_bad(response, "Informe um telefone válido com DDD.")

    for email in ["a@a", "teste@", "@gmail.com", "teste@gmail", "teste@.com"]:
        response = client.post(
            "/api/partner-applications",
            json={
                "full_name": "Passeador Validacao",
                "cpf": unique_cpf(),
                "phone": VALID_PHONE,
                "email": email,
                "accepted_declaration": True,
                **WALKER_COMPLETE_FIELDS,
            },
        )
        expect_bad(response, "Informe um e-mail válido.")

    walker_valid_cpf = unique_cpf()
    walker_valid_phone = unique_mobile_phone()
    walker_valid = client.post(
        "/api/partner-applications",
        json={
            "full_name": "Passeador Validacao Real",
            "cpf": walker_valid_cpf,
            "phone": walker_valid_phone,
            "email": unique_email("walker-valid"),
            "accepted_declaration": True,
            **WALKER_COMPLETE_FIELDS,
        },
    )
    assert walker_valid.status_code == 201, walker_valid.text
    assert walker_valid.json()["cpf"] == walker_valid_cpf
    assert walker_valid.json()["phone"] == walker_valid_phone

    walker_landline_phone = unique_landline_phone()
    walker_landline = client.post(
        "/api/partner-applications",
        json={
            "full_name": "Passeador Telefone Fixo",
            "cpf": unique_cpf(),
            "phone": walker_landline_phone,
            "email": unique_email("walker-landline"),
            "accepted_declaration": True,
            **WALKER_COMPLETE_FIELDS,
        },
    )
    assert walker_landline.status_code == 201, walker_landline.text
    assert walker_landline.json()["phone"] == walker_landline_phone

    tutor_invalid = client.post(
        "/auth/register",
        json={
            "full_name": "Tutor CPF Invalido",
            "email": unique_email("tutor-invalid"),
            "password": "Senha123",
            "role": "cliente",
            "profile": {"personal": {"cpf": "12345678900", "telefone": VALID_PHONE}},
        },
    )
    expect_bad(tutor_invalid, "Informe um CPF válido.")

    tutor_valid = client.post(
        "/auth/register",
        json={
            "full_name": "Tutor Validacao Real",
            "email": VALID_EMAIL.replace("@", f".{datetime.utcnow().strftime('%H%M%S%f')}@"),
            "password": "Senha123",
            "role": "cliente",
            "profile": {"personal": {"cpf": unique_cpf(), "telefone": unique_mobile_phone()}},
        },
    )
    assert tutor_valid.status_code == 200, tutor_valid.text

    print("registration validation simulation ok")


if __name__ == "__main__":
    main()
