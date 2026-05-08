import os
from datetime import UTC, datetime

os.environ["EXPO_PUBLIC_DEMO_MODE"] = "false"
os.environ["DEMO_MODE"] = "false"

from fastapi.testclient import TestClient

from app.core.database import SessionLocal
from app.core.security import create_access_token, get_password_hash
from app.main import app
from app.models.user import User


def ensure_admin() -> str:
    email = "admin-activation-flow@aumigao.local"
    with SessionLocal() as db:
        user = db.query(User).filter(User.email == email).first()
        if not user:
            user = User(
                id="admin-activation-flow",
                email=email,
                password_hash=get_password_hash("AdminActivation123"),
                full_name="Admin Activation Flow",
                role="admin",
                is_active=True,
            )
            db.add(user)
            db.commit()
    return create_access_token("admin-activation-flow", {"role": "admin"})


def cpf_from_seed(seed: int) -> str:
    base = f"{seed % 1_000_000_000:09d}"
    for weight_start in (10, 11):
        total = sum(int(digit) * weight for digit, weight in zip(base, range(weight_start, 1, -1)))
        check = (total * 10) % 11
        base += str(0 if check == 10 else check)
    return base


def phone_from_seed(seed: int) -> str:
    return f"719{seed % 100_000_000:08d}"


def application_payload(email: str, name: str, *, complete: bool = True, seed: int = 1) -> dict:
    payload = {
        "full_name": name,
        "cpf": cpf_from_seed(seed),
        "phone": phone_from_seed(seed),
        "email": email,
        "neighborhood_region": "Pituba",
        "has_pet_experience": True,
        "has_third_party_experience": True,
        "experience_description": "Cadastro completo para validacao do fluxo real.",
        "availability": "Segunda a sexta",
        "accepted_declaration": True,
    }
    if complete:
        payload.update({
            "profile_photo_url": "beta://profile-photo",
            "document_url": "beta://identity-document",
            "proof_of_address_url": "beta://proof-of-address",
        })
    return payload


def public_names(client: TestClient, name: str) -> list[dict]:
    response = client.get("/walker/public")
    assert response.status_code == 200, response.text
    return [item for item in response.json().get("walkers", []) if item.get("name") == name]


def matching_names(client: TestClient, headers: dict, name: str) -> list[dict]:
    response = client.post(
        "/matching/walkers",
        json={"city": "Pituba", "neighborhood": "Pituba", "scheduled_at": "2026-05-09T10:00:00", "duration_minutes": 45},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    return [item for item in payload.get("top_recommended", []) + payload.get("other_options", []) if item.get("name") == name]


def main():
    client = TestClient(app)
    admin_headers = {"Authorization": f"Bearer {ensure_admin()}"}
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S%f")
    seed_base = int(stamp[-9:])

    incomplete = client.post(
        "/api/partner-applications",
        json=application_payload(f"incomplete-{stamp}@aumigao.local", f"Passeador Incompleto {stamp}", complete=False, seed=seed_base + 1),
    )
    assert incomplete.status_code == 400, incomplete.text
    incomplete_detail = incomplete.json()["detail"]
    assert "Envie sua foto de perfil." in incomplete_detail["errors"], incomplete_detail
    assert "Envie o documento obrigatório." in incomplete_detail["errors"], incomplete_detail

    pending_name = f"Passeador Pendente {stamp}"
    pending = client.post(
        "/api/partner-applications",
        json=application_payload(f"pending-{stamp}@aumigao.local", pending_name, seed=seed_base + 2),
    )
    assert pending.status_code == 201, pending.text
    pending_data = pending.json()
    assert pending_data["raw_status"] in {"pending", "document_review"}, pending_data
    assert pending_data["active_as_walker"] is False, pending_data
    assert not public_names(client, pending_name)
    pending_headers = {"Authorization": f"Bearer {create_access_token(pending_data['user_id'], {'role': 'cliente'})}"}
    assert client.get("/walker/dashboard", headers=pending_headers).status_code == 403

    approved_name = f"Passeador Ativado {stamp}"
    approved_candidate = client.post(
        "/api/partner-applications",
        json=application_payload(f"approved-{stamp}@aumigao.local", approved_name, seed=seed_base + 3),
    ).json()
    approved = client.post(f"/admin/walkers/{approved_candidate['id']}/approve", headers=admin_headers)
    assert approved.status_code == 200, approved.text
    approved_data = approved.json()
    assert approved_data["raw_status"] == "active", approved_data
    assert approved_data["active_as_walker"] is True, approved_data

    approved_headers = {"Authorization": f"Bearer {create_access_token(approved_data['user_id'], {'role': 'walker'})}"}
    assert client.get("/walker/dashboard", headers=approved_headers).status_code == 200
    assert client.get("/walker/requests", headers=approved_headers).status_code == 200
    assert len(public_names(client, approved_name)) == 1
    assert len(matching_names(client, admin_headers, approved_name)) == 1

    rejected_name = f"Passeador Rejeitado {stamp}"
    rejected_candidate = client.post(
        "/api/partner-applications",
        json=application_payload(f"rejected-{stamp}@aumigao.local", rejected_name, seed=seed_base + 4),
    ).json()
    rejected = client.post(
        f"/admin/walkers/{rejected_candidate['id']}/reject",
        json={"reason": "Documento ilegivel."},
        headers=admin_headers,
    )
    assert rejected.status_code == 200, rejected.text
    rejected_data = rejected.json()
    assert rejected_data["raw_status"] == "rejected", rejected_data
    assert rejected_data["active_as_walker"] is False, rejected_data
    assert not public_names(client, rejected_name)
    assert not matching_names(client, admin_headers, rejected_name)

    print("walker activation flow simulation ok")


if __name__ == "__main__":
    main()
