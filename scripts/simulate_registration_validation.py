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


def unique_email(prefix: str) -> str:
    stamp = datetime.utcnow().strftime("%Y%m%d%H%M%S%f")
    return f"{prefix}-{stamp}@gmail.com"


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
            },
        )
        expect_bad(response, "Informe um CPF válido.")

    for phone in ["999", "11111111111", "719999999999999"]:
        response = client.post(
            "/api/partner-applications",
            json={
                "full_name": "Passeador Validacao",
                "cpf": VALID_CPF,
                "phone": phone,
                "email": unique_email("walker-phone-invalid"),
                "accepted_declaration": True,
            },
        )
        expect_bad(response, "Informe um telefone válido com DDD.")

    for email in ["a@a", "teste@", "@gmail.com", "teste@gmail", "teste@.com"]:
        response = client.post(
            "/api/partner-applications",
            json={
                "full_name": "Passeador Validacao",
                "cpf": VALID_CPF,
                "phone": VALID_PHONE,
                "email": email,
                "accepted_declaration": True,
            },
        )
        expect_bad(response, "Informe um e-mail válido.")

    walker_valid = client.post(
        "/api/partner-applications",
        json={
            "full_name": "Passeador Validacao Real",
            "cpf": VALID_CPF,
            "phone": VALID_PHONE,
            "email": unique_email("walker-valid"),
            "accepted_declaration": True,
        },
    )
    assert walker_valid.status_code == 201, walker_valid.text
    assert walker_valid.json()["cpf"] == VALID_CPF
    assert walker_valid.json()["phone"] == VALID_PHONE

    walker_landline = client.post(
        "/api/partner-applications",
        json={
            "full_name": "Passeador Telefone Fixo",
            "cpf": VALID_CPF,
            "phone": VALID_LANDLINE,
            "email": unique_email("walker-landline"),
            "accepted_declaration": True,
        },
    )
    assert walker_landline.status_code == 201, walker_landline.text
    assert walker_landline.json()["phone"] == VALID_LANDLINE

    tutor_invalid = client.post(
        "/auth/register",
        json={
            "full_name": "Tutor CPF Invalido",
            "email": unique_email("tutor-invalid"),
            "password": "123456",
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
            "password": "123456",
            "role": "cliente",
            "profile": {"personal": {"cpf": VALID_CPF, "telefone": VALID_PHONE}},
        },
    )
    assert tutor_valid.status_code == 200, tutor_valid.text

    print("registration validation simulation ok")


if __name__ == "__main__":
    main()
