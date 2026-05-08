import os
from datetime import datetime

os.environ["EXPO_PUBLIC_DEMO_MODE"] = "false"
os.environ["DEMO_MODE"] = "false"

from fastapi.testclient import TestClient

from app.main import app


def unique_email(prefix: str) -> str:
    return f"{prefix}-{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}@gmail.com"


def unique_cpf() -> str:
    base = datetime.utcnow().strftime("%H%M%S%f")[-9:]

    def digit(numbers: str) -> str:
        factor = len(numbers) + 1
        total = sum(int(number) * (factor - index) for index, number in enumerate(numbers))
        value = (total * 10) % 11
        return "0" if value == 10 else str(value)

    first = digit(base)
    return base + first + digit(base + first)


def unique_phone() -> str:
    return "719" + datetime.utcnow().strftime("%H%M%S%f")[-8:]


def main():
    client = TestClient(app)
    password = "Senha123"
    bio = "Sou apaixonado por pets, tenho rotina organizada, cuido com carinho e mantenho tutores informados durante todo o passeio."
    base_payload = {
        "full_name": "Passeador Fluxo Real",
        "cpf": unique_cpf(),
        "phone": unique_phone(),
        "email": unique_email("walker-flow"),
        "accepted_declaration": True,
        "profile_photo_url": "https://example.com/profile.jpg",
        "document_url": "https://example.com/identity-front.jpg",
        "identity_document_back_url": "https://example.com/identity-back.jpg",
        "proof_of_address_url": "https://example.com/address.jpg",
        "bio": bio,
        "experience_options": ["Tenho pet(s)", "Ja cuidei de pets de familiares/amigos"],
    }

    no_password = client.post("/api/partner-applications", json=base_payload)
    assert no_password.status_code == 400, no_password.text

    weak_password = client.post("/api/partner-applications", json={**base_payload, "password": "1234567"})
    assert weak_password.status_code == 400, weak_password.text

    only_front = client.post(
        "/api/partner-applications",
        json={
            **base_payload,
            "cpf": unique_cpf(),
            "phone": unique_phone(),
            "email": unique_email("walker-front-only"),
            "password": password,
            "identity_document_back_url": "",
        },
    )
    assert only_front.status_code == 400 and "verso" in only_front.text.lower(), only_front.text

    walker_email = unique_email("walker-ok")
    created = client.post(
        "/api/partner-applications",
        json={
            **base_payload,
            "cpf": unique_cpf(),
            "phone": unique_phone(),
            "email": walker_email,
            "password": password,
        },
    )
    assert created.status_code == 201, created.text
    candidate = created.json()
    assert candidate["raw_status"] == "document_review", candidate

    pending_public = client.get("/walker/public")
    assert pending_public.status_code == 200, pending_public.text
    assert not any(item.get("partner_id") == candidate["id"] for item in pending_public.json()["walkers"])

    login = client.post("/auth/login", json={"email": walker_email, "password": password})
    assert login.status_code == 200, login.text
    walker_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    profile_before = client.get("/walker/profile", headers=walker_headers)
    assert profile_before.status_code == 200, profile_before.text
    assert profile_before.json()["status"] == "document_review", profile_before.text
    dashboard_before = client.get("/walker/dashboard", headers=walker_headers)
    assert dashboard_before.status_code == 403, dashboard_before.text

    admin_login = client.post("/auth/login", json={"email": "admin@petpasso.com", "password": "Admin@123"})
    assert admin_login.status_code == 200, admin_login.text
    admin_headers = {"Authorization": f"Bearer {admin_login.json()['access_token']}"}

    admin_detail = client.get(f"/admin/partner-applications/{candidate['id']}", headers=admin_headers)
    assert admin_detail.status_code == 200, admin_detail.text
    admin_json = admin_detail.json()
    assert admin_json["identity_document_front_url"].startswith("https://")
    assert admin_json["identity_document_back_url"].startswith("https://")

    approved = client.post(f"/admin/walkers/{candidate['id']}/approve", headers=admin_headers)
    assert approved.status_code == 200, approved.text
    approved_json = approved.json()
    assert approved_json["raw_status"] == "active", approved.text
    assert approved_json["active_as_walker"] is True, approved.text

    profile_after = client.get("/walker/profile", headers=walker_headers)
    assert profile_after.status_code == 200, profile_after.text
    assert profile_after.json()["status"] == "active", profile_after.text
    assert profile_after.json()["active_as_walker"] is True, profile_after.text

    public_walkers = client.get("/walker/public")
    assert public_walkers.status_code == 200, public_walkers.text
    assert any(item["partner_id"] == candidate["id"] for item in public_walkers.json()["walkers"])

    print("walker password documents status simulation ok")


if __name__ == "__main__":
    main()
