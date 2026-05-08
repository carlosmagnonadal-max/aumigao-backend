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
    email = "admin-sim-flow@aumigao.local"
    with SessionLocal() as db:
        user = db.query(User).filter(User.email == email).first()
        if not user:
            user = User(
                id="admin-sim-flow",
                email=email,
                password_hash=get_password_hash("AdminSim123"),
                full_name="Admin Sim Flow",
                role="admin",
                is_active=True,
            )
            db.add(user)
            db.commit()
    return create_access_token("admin-sim-flow", {"role": "admin"})


def public_matches(payload: dict, name: str) -> list[dict]:
    return [item for item in payload.get("walkers", []) if item.get("name") == name]


def matching_matches(payload: dict, name: str) -> list[dict]:
    rows = payload.get("top_recommended", []) + payload.get("other_options", [])
    return [item for item in rows if item.get("name") == name]


def cpf_from_seed(seed: int) -> str:
    base = f"{seed % 1_000_000_000:09d}"
    for weight_start in (10, 11):
        total = sum(int(digit) * weight for digit, weight in zip(base, range(weight_start, 1, -1)))
        check = (total * 10) % 11
        base += str(0 if check == 10 else check)
    return base


def phone_from_seed(seed: int) -> str:
    return f"719{seed % 100_000_000:08d}"


def create_application(client: TestClient, *, email: str, name: str, seed: int, photo_url: str = "beta://profile-photo") -> dict:
    created = client.post(
        "/api/partner-applications",
        json={
            "full_name": name,
            "cpf": cpf_from_seed(seed),
            "phone": phone_from_seed(seed),
            "email": email,
            "neighborhood_region": "Pituba",
            "has_pet_experience": True,
            "has_third_party_experience": True,
            "experience_description": "Cadastro real para auditoria admin.",
            "availability": "Segunda a sexta",
            "profile_photo_url": photo_url,
            "document_url": "beta://identity-document",
            "proof_of_address_url": "beta://proof-of-address",
            "accepted_declaration": True,
        },
    )
    assert created.status_code == 201, created.text
    return created.json()


def assert_not_public_or_matching(client: TestClient, admin_headers: dict, name: str):
    public_walkers = client.get("/walker/public")
    assert public_walkers.status_code == 200, public_walkers.text
    assert not public_matches(public_walkers.json(), name), public_walkers.json()

    matching = client.post(
        "/matching/walkers",
        json={"city": "Pituba", "neighborhood": "Pituba", "scheduled_at": "2026-05-09T10:00:00", "duration_minutes": 45},
        headers=admin_headers,
    )
    assert matching.status_code == 200, matching.text
    assert not matching_matches(matching.json(), name), matching.json()


def main():
    client = TestClient(app)
    token = ensure_admin()
    admin_headers = {"Authorization": f"Bearer {token}"}
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S%f")
    seed_base = int(stamp[-9:])
    approved_name = f"Passeador Auditoria Real {stamp}"
    approved_email = f"walker-flow-{stamp}@aumigao.local"

    candidate = create_application(
        client,
        email=approved_email,
        name=approved_name,
        seed=seed_base + 1,
        photo_url="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB",
    )
    assert candidate["raw_status"] in {"pending", "document_review"}, candidate
    assert candidate["active_as_walker"] is False, candidate
    assert_not_public_or_matching(client, admin_headers, approved_name)

    admin_list = client.get("/admin/walkers", headers=admin_headers)
    assert admin_list.status_code == 200, admin_list.text
    assert any(item["id"] == candidate["id"] for item in admin_list.json())

    approved = client.post(f"/admin/walkers/{candidate['id']}/approve", headers=admin_headers)
    assert approved.status_code == 200, approved.text
    approved_data = approved.json()
    assert approved_data["raw_status"] == "active", approved_data
    assert approved_data["active_as_walker"] is True, approved_data

    active_list = client.get("/admin/walkers", headers=admin_headers).json()
    assert any(item["id"] == candidate["id"] and item["raw_status"] == "active" for item in active_list)

    public_walkers = client.get("/walker/public")
    assert public_walkers.status_code == 200, public_walkers.text
    assert len(public_matches(public_walkers.json(), approved_name)) == 1, public_walkers.json()

    api_walkers = client.get("/api/walkers")
    assert api_walkers.status_code == 200, api_walkers.text
    assert len([item for item in api_walkers.json() if item.get("name") == approved_name]) == 1, api_walkers.json()

    matching = client.post(
        "/matching/walkers",
        json={"city": "Pituba", "neighborhood": "Pituba", "scheduled_at": "2026-05-09T10:00:00", "duration_minutes": 45},
        headers=admin_headers,
    )
    assert matching.status_code == 200, matching.text
    assert len(matching_matches(matching.json(), approved_name)) == 1, matching.json()

    rejected_name = f"Passeador Rejeitado Auditoria {stamp}"
    rejected_candidate = create_application(
        client,
        email=f"walker-reject-{stamp}@aumigao.local",
        name=rejected_name,
        seed=seed_base + 2,
    )
    rejected = client.post(
        f"/admin/walkers/{rejected_candidate['id']}/reject",
        json={"reason": "Documentos insuficientes"},
        headers=admin_headers,
    )
    assert rejected.status_code == 200, rejected.text
    rejected_data = rejected.json()
    assert rejected_data["raw_status"] == "rejected", rejected_data
    assert rejected_data["active_as_walker"] is False, rejected_data
    assert_not_public_or_matching(client, admin_headers, rejected_name)

    dashboard = client.get("/admin/dashboard", headers=admin_headers)
    assert dashboard.status_code == 200, dashboard.text
    dashboard_data = dashboard.json()
    assert dashboard_data["total_active_walkers"] >= 1
    assert dashboard_data["estimated_revenue_paid"] >= 0

    print("admin walker flow simulation ok")


if __name__ == "__main__":
    main()
