import os
from datetime import UTC, datetime

os.environ["EXPO_PUBLIC_DEMO_MODE"] = "false"
os.environ["DEMO_MODE"] = "false"

from fastapi.testclient import TestClient

from app.core.database import SessionLocal
from app.core.security import create_access_token, get_password_hash
from app.main import app
from app.models.user import User
from app.models.walker_profile import WalkerProfile


def ensure_admin() -> str:
    email = "admin-docs-duplicates@aumigao.local"
    with SessionLocal() as db:
        user = db.query(User).filter(User.email == email).first()
        if not user:
            user = User(
                id="admin-docs-duplicates",
                email=email,
                password_hash=get_password_hash("AdminDocs123"),
                full_name="Admin Docs Duplicates",
                role="admin",
                is_active=True,
            )
            db.add(user)
            db.commit()
    return create_access_token("admin-docs-duplicates", {"role": "admin"})


def cpf_from_seed(seed: int) -> str:
    base = f"{seed % 1_000_000_000:09d}"
    for weight_start in (10, 11):
        total = sum(int(digit) * weight for digit, weight in zip(base, range(weight_start, 1, -1)))
        check = (total * 10) % 11
        base += str(0 if check == 10 else check)
    return base


def phone_from_seed(seed: int) -> str:
    return f"719{seed % 100_000_000:08d}"


def application_payload(email: str, name: str, seed: int, *, cpf: str | None = None, phone: str | None = None) -> dict:
    return {
        "full_name": name,
        "cpf": cpf or cpf_from_seed(seed),
        "phone": phone or phone_from_seed(seed),
        "email": email,
        "neighborhood_region": "Pituba",
        "has_pet_experience": True,
        "has_third_party_experience": True,
        "experience_description": "Candidatura completa com documentos beta.",
        "availability": "Segunda a sexta",
        "profile_photo_url": "beta://profile-photo",
        "document_url": "beta://identity-document",
        "proof_of_address_url": "beta://proof-of-address",
        "selfie_url": "beta://selfie",
        "accepted_declaration": True,
    }


def assert_conflict(response, expected: str):
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == expected, response.text


def main():
    client = TestClient(app)
    admin_headers = {"Authorization": f"Bearer {ensure_admin()}"}
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S%f")
    seed_base = int(stamp[-9:])

    email = f"docs-flow-{stamp}@aumigao.local"
    name = f"Passeador Docs {stamp}"
    payload = application_payload(email, name, seed_base + 1)

    created = client.post("/api/partner-applications", json=payload)
    assert created.status_code == 201, created.text
    candidate = created.json()
    assert candidate["raw_status"] == "document_review", candidate
    assert candidate["active_as_walker"] is False, candidate

    detail = client.get(f"/admin/partner-applications/{candidate['id']}", headers=admin_headers)
    assert detail.status_code == 200, detail.text
    detail_data = detail.json()
    assert detail_data["profile_photo_url"] == "beta://profile-photo", detail_data
    assert detail_data["document_url"] == "beta://identity-document", detail_data
    assert detail_data["proof_of_address_url"] == "beta://proof-of-address", detail_data
    assert detail_data["selfie_url"] == "beta://selfie", detail_data

    pending_headers = {"Authorization": f"Bearer {create_access_token(candidate['user_id'], {'role': 'cliente'})}"}
    assert client.get("/walker/dashboard", headers=pending_headers).status_code == 403

    approved = client.post(f"/admin/walkers/{candidate['id']}/approve", headers=admin_headers)
    assert approved.status_code == 200, approved.text
    approved_data = approved.json()
    assert approved_data["raw_status"] == "active", approved_data
    assert approved_data["active_as_walker"] is True, approved_data

    active_headers = {"Authorization": f"Bearer {create_access_token(candidate['user_id'], {'role': 'walker'})}"}
    active_profile = client.get("/walker/profile", headers=active_headers)
    assert active_profile.status_code == 200, active_profile.text
    assert active_profile.json()["status"] == "active", active_profile.text
    assert client.get("/walker/dashboard", headers=active_headers).status_code == 200

    duplicate_cpf = client.post(
        "/api/partner-applications",
        json=application_payload(f"dup-cpf-{stamp}@aumigao.local", f"Dup CPF {stamp}", seed_base + 2, cpf=payload["cpf"]),
    )
    assert_conflict(duplicate_cpf, "Este CPF já está cadastrado.")

    duplicate_phone = client.post(
        "/api/partner-applications",
        json=application_payload(f"dup-phone-{stamp}@aumigao.local", f"Dup Telefone {stamp}", seed_base + 3, phone=payload["phone"]),
    )
    assert_conflict(duplicate_phone, "Este telefone já está cadastrado.")

    duplicate_email = client.post(
        "/api/partner-applications",
        json=application_payload(email, f"Dup Email {stamp}", seed_base + 4),
    )
    assert_conflict(duplicate_email, "Este e-mail já está cadastrado.")

    login_email = f"login-active-{stamp}@gmail.com"
    login_password = "WalkerLogin123"
    registered = client.post(
        "/auth/register",
        json={
            "full_name": f"Passeador Login {stamp}",
            "email": login_email,
            "password": login_password,
            "role": "passeador",
            "profile": {
                "personal": {"cpf": cpf_from_seed(seed_base + 5), "telefone": phone_from_seed(seed_base + 5)},
                "profile": {"photoUri": "beta://profile-photo-login"},
                "documents": {"identity": "beta://identity-login", "residence": "beta://residence-login", "petPhoto": "beta://selfie-login"},
            },
        },
    )
    assert registered.status_code == 200, registered.text
    with SessionLocal() as db:
        user = db.query(User).filter(User.email == login_email).first()
        profile = db.query(WalkerProfile).filter(WalkerProfile.user_id == user.id).first()
        profile_id = profile.id
    approved_login = client.post(f"/admin/walkers/{profile_id}/approve", headers=admin_headers)
    assert approved_login.status_code == 200, approved_login.text
    login = client.post("/auth/login", json={"email": login_email, "password": login_password})
    assert login.status_code == 200, login.text
    assert login.json()["user"]["role"] == "walker", login.text

    print("walker approval documents duplicates simulation ok")


if __name__ == "__main__":
    main()
