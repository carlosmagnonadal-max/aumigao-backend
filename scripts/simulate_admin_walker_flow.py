import os
from datetime import datetime

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


def main():
    client = TestClient(app)
    token = ensure_admin()
    admin_headers = {"Authorization": f"Bearer {token}"}
    stamp = datetime.utcnow().strftime("%Y%m%d%H%M%S%f")
    email = f"walker-flow-{stamp}@aumigao.local"

    created = client.post(
        "/api/partner-applications",
        json={
            "full_name": "Passeador Auditoria Real",
            "cpf": "52998224725",
            "phone": "71999990000",
            "email": email,
            "neighborhood_region": "Pituba",
            "has_pet_experience": True,
            "has_third_party_experience": True,
            "experience_description": "Cadastro real para auditoria admin.",
            "availability": "Segunda a sexta",
            "profile_photo_url": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB",
            "accepted_declaration": True,
        },
    )
    assert created.status_code == 201, created.text
    candidate = created.json()
    assert candidate["status"] in {"Em análise", "Aprovação documental"}, candidate

    admin_list = client.get("/admin/walkers", headers=admin_headers)
    assert admin_list.status_code == 200, admin_list.text
    assert any(item["id"] == candidate["id"] for item in admin_list.json())

    approved = client.post(f"/admin/walkers/{candidate['id']}/approve", headers=admin_headers)
    assert approved.status_code == 200, approved.text
    approved_data = approved.json()
    assert approved_data["active_as_walker"] is True

    active_list = client.get("/admin/walkers", headers=admin_headers).json()
    assert any(item["id"] == candidate["id"] and item["raw_status"] == "active" for item in active_list)

    public_walkers = client.get("/walker/public")
    assert public_walkers.status_code == 200, public_walkers.text
    assert any(item["name"] == "Passeador Auditoria Real" for item in public_walkers.json()["walkers"])

    matching = client.post(
        "/matching/walkers",
        json={"city": "Pituba", "neighborhood": "Pituba", "scheduled_at": "2026-05-09T10:00:00", "duration_minutes": 45},
        headers=admin_headers,
    )
    assert matching.status_code == 200, matching.text
    matching_data = matching.json()
    found = matching_data["top_recommended"] + matching_data["other_options"]
    assert any(item["name"] == "Passeador Auditoria Real" for item in found), matching_data

    dashboard = client.get("/admin/dashboard", headers=admin_headers)
    assert dashboard.status_code == 200, dashboard.text
    dashboard_data = dashboard.json()
    assert dashboard_data["total_active_walkers"] >= 1
    assert dashboard_data["estimated_revenue_paid"] >= 0

    print("admin walker flow simulation ok")


if __name__ == "__main__":
    main()
