import uuid
from datetime import datetime, timedelta, timezone

import pytest
import requests


# Module coverage: API health and core walk flows (create, list, detail, status, photo)
class TestWalksApi:
    def _build_payload(self, suffix: str, dt: datetime):
        return {
            "pet_name": f"TEST_Pet_{suffix}",
            "client_name": f"TEST_Client_{suffix}",
            "walk_date": dt.strftime("%Y-%m-%d"),
            "walk_time": dt.strftime("%H:%M"),
            "duration_minutes": 30,
            "walk_type": "Individual",
            "walker_id": "walker-1",
            "pickup_street": "Rua das Flores",
            "pickup_number": "120",
            "pickup_neighborhood": "Centro",
            "pickup_complement": "Apto 12",
            "location_reference": "Próximo à praça central",
            "notes": "TEST_Note",
        }

    def _build_partner_payload(self, suffix: str):
        return {
            "full_name": f"TEST_Partner_{suffix}",
            "phone": "71999990000",
            "email": f"partner_{suffix}@test.com",
            "neighborhood_region": "Pituba",
            "has_pet_experience": True,
            "has_third_party_experience": True,
            "experience_description": "TEST experiência com passeios e cuidado diário.",
            "availability": "Segunda a sexta, das 8h às 18h",
            "profile_photo_url": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB",
            "accepted_declaration": True,
        }

    def _build_pet_payload(self, suffix: str, behavior: str = "Neutro"):
        return {
            "pet_name": f"TEST_PetProfile_{suffix}",
            "behavioral_notes": "TEST comportamento",
            "photo_url": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB",
            "owner_name": f"TEST_Tutor_{suffix}",
            "gets_along_with_dogs": True,
            "accepts_shared_walk": True,
            "pet_size": "Médio",
            "energy_level": "Médio",
            "pulls_leash": False,
            "dog_behavior": behavior,
        }

    def _build_shared_walk_payload(
        self,
        suffix: str,
        dt: datetime,
        pet_id: str,
        second_pet_id: str,
        shared_context: str = "same_household",
    ):
        payload = self._build_payload(suffix, dt)
        payload["walk_type"] = "Compartilhado"
        payload["shared_context"] = shared_context
        payload["pet_id"] = pet_id
        payload["second_pet_id"] = second_pet_id
        payload["duration_minutes"] = 45
        return payload

    def test_api_root(self, api_client, base_url):
        response = api_client.get(f"{base_url}/api/")
        assert response.status_code == 200
        assert response.json().get("message") == "PetPasso API ativa"

    def test_walkers_list(self, api_client, base_url):
        response = api_client.get(f"{base_url}/api/walkers")
        assert response.status_code == 200
        walkers = response.json()
        assert len(walkers) >= 1
        assert walkers[0]["id"]
        assert walkers[0]["name"]
        assert walkers[0]["photo_url"].startswith("data:image/")

    def test_create_walk_and_get_persistence(self, api_client, base_url):
        payload = self._build_payload("create", datetime.now(timezone.utc) + timedelta(days=1))
        create_response = api_client.post(f"{base_url}/api/walks", json=payload)

        assert create_response.status_code == 201
        created = create_response.json()
        assert created["pet_name"] == payload["pet_name"]
        assert created["status"] == "Agendado"
        assert created["walker_id"] == "walker-1"
        assert created["pickup_street"] == "Rua das Flores"

        walk_id = created["id"]
        get_response = api_client.get(f"{base_url}/api/walks/{walk_id}")
        assert get_response.status_code == 200
        fetched = get_response.json()
        assert fetched["id"] == walk_id
        assert fetched["client_name"] == payload["client_name"]
        assert fetched["walker_id"] == payload["walker_id"]
        assert fetched["pickup_number"] == payload["pickup_number"]
        assert fetched["pickup_neighborhood"] == payload["pickup_neighborhood"]
        assert fetched["pickup_complement"] == payload["pickup_complement"]
        assert fetched["location_reference"] == payload["location_reference"]
        assert fetched["walker_name"]
        assert fetched["walker_photo_url"].startswith("data:image/")

    @pytest.mark.parametrize("duration", [45, 60])
    def test_create_walk_accepts_new_durations_and_persists_neighborhood(self, api_client, base_url, duration):
        payload = self._build_payload(f"duration_{duration}", datetime.now(timezone.utc) + timedelta(days=1))
        payload["duration_minutes"] = duration
        payload["pickup_neighborhood"] = f"TEST_Bairro_{duration}"
        payload["location_reference"] = ""

        create_response = api_client.post(f"{base_url}/api/walks", json=payload)
        assert create_response.status_code == 201

        created = create_response.json()
        assert created["duration_minutes"] == duration
        assert created["pickup_neighborhood"] == payload["pickup_neighborhood"]
        assert created["location_reference"] == payload["pickup_neighborhood"]

        walk_id = created["id"]
        get_response = api_client.get(f"{base_url}/api/walks/{walk_id}")
        assert get_response.status_code == 200
        fetched = get_response.json()
        assert fetched["duration_minutes"] == duration
        assert fetched["pickup_neighborhood"] == payload["pickup_neighborhood"]

    def test_list_walks_grouping_data_is_sortable_by_proximity(self, api_client, base_url):
        now = datetime.now(timezone.utc) + timedelta(days=2)
        payload_later = self._build_payload("later", now + timedelta(hours=2))
        payload_earlier = self._build_payload("earlier", now + timedelta(hours=1))

        later_response = api_client.post(f"{base_url}/api/walks", json=payload_later)
        earlier_response = api_client.post(f"{base_url}/api/walks", json=payload_earlier)
        assert later_response.status_code == 201
        assert earlier_response.status_code == 201

        later_id = later_response.json()["id"]
        earlier_id = earlier_response.json()["id"]

        list_response = api_client.get(f"{base_url}/api/walks")
        assert list_response.status_code == 200
        walks = list_response.json()
        ids = [walk["id"] for walk in walks]
        assert ids.index(earlier_id) < ids.index(later_id)

    def test_status_transition_full_flow_to_finalizado(self, api_client, base_url):
        payload = self._build_payload("status", datetime.now(timezone.utc) + timedelta(days=3))
        create_response = api_client.post(f"{base_url}/api/walks", json=payload)
        assert create_response.status_code == 201

        walk_id = create_response.json()["id"]

        going_pickup = api_client.patch(
            f"{base_url}/api/walks/{walk_id}/status", json={"status": "Indo buscar o pet"}
        )
        assert going_pickup.status_code == 200
        assert going_pickup.json()["status"] == "Indo buscar o pet"

        walking = api_client.patch(
            f"{base_url}/api/walks/{walk_id}/status", json={"status": "Passeando agora"}
        )
        assert walking.status_code == 200
        assert walking.json()["status"] == "Passeando agora"

        finished = api_client.patch(
            f"{base_url}/api/walks/{walk_id}/status", json={"status": "Finalizado"}
        )
        assert finished.status_code == 200
        assert finished.json()["status"] == "Finalizado"
        assert "passeou por" in finished.json()["summary_text"]

        persisted = api_client.get(f"{base_url}/api/walks/{walk_id}")
        assert persisted.status_code == 200
        assert persisted.json()["status"] == "Finalizado"

    def test_security_code_persists_after_status_updates(self, api_client, base_url):
        payload = self._build_payload("security", datetime.now(timezone.utc) + timedelta(days=3))
        create_response = api_client.post(f"{base_url}/api/walks", json=payload)
        assert create_response.status_code == 201

        created_walk = create_response.json()
        walk_id = created_walk["id"]
        security_code = created_walk["security_code"]
        assert len(security_code) == 4
        assert security_code.isdigit()

        _ = api_client.patch(f"{base_url}/api/walks/{walk_id}/status", json={"status": "Indo buscar o pet"})
        _ = api_client.patch(f"{base_url}/api/walks/{walk_id}/status", json={"status": "Passeando agora"})
        _ = api_client.patch(f"{base_url}/api/walks/{walk_id}/status", json={"status": "Finalizado"})

        get_response = api_client.get(f"{base_url}/api/walks/{walk_id}")
        assert get_response.status_code == 200
        assert get_response.json()["security_code"] == security_code

    def test_update_experience_and_rating_after_finish(self, api_client, base_url):
        payload = self._build_payload("experience", datetime.now(timezone.utc) + timedelta(days=3))
        create_response = api_client.post(f"{base_url}/api/walks", json=payload)
        assert create_response.status_code == 201
        walk_id = create_response.json()["id"]

        experience = api_client.patch(
            f"{base_url}/api/walks/{walk_id}/experience", json={"did_pee": True, "did_poop": True}
        )
        assert experience.status_code == 200
        assert experience.json()["did_pee"] is True
        assert experience.json()["did_poop"] is True

        rating_before_finish = api_client.patch(
            f"{base_url}/api/walks/{walk_id}/rating", json={"rating": 5, "comment": "Ótimo"}
        )
        assert rating_before_finish.status_code == 400

        _ = api_client.patch(f"{base_url}/api/walks/{walk_id}/status", json={"status": "Indo buscar o pet"})
        _ = api_client.patch(f"{base_url}/api/walks/{walk_id}/status", json={"status": "Passeando agora"})
        _ = api_client.patch(f"{base_url}/api/walks/{walk_id}/status", json={"status": "Finalizado"})

        updated_experience_after_finish = api_client.patch(
            f"{base_url}/api/walks/{walk_id}/experience", json={"did_pee": True, "did_poop": False}
        )
        assert updated_experience_after_finish.status_code == 200
        assert "Fez apenas xixi." in updated_experience_after_finish.json()["summary_text"]

        rating = api_client.patch(
            f"{base_url}/api/walks/{walk_id}/rating", json={"rating": 4, "comment": "Muito bom"}
        )
        assert rating.status_code == 200
        assert rating.json()["rating"] == 4
        assert rating.json()["rating_comment"] == "Muito bom"

    def test_upsert_and_get_pet_profile(self, api_client, base_url):
        put_response = api_client.put(
            f"{base_url}/api/pet-profile",
            json={"pet_name": "TEST_Pet_Profile", "behavioral_notes": "TEST_Behavior"},
        )
        assert put_response.status_code == 200
        assert put_response.json()["pet_name"] == "TEST_Pet_Profile"

        get_response = api_client.get(f"{base_url}/api/pet-profile")
        assert get_response.status_code == 200
        assert get_response.json()["behavioral_notes"] == "TEST_Behavior"

    def test_upsert_and_get_owner_profile(self, api_client, base_url):
        response = api_client.put(
            f"{base_url}/api/owner-profile",
            json={
                "full_name": "TEST_Owner",
                "phone": "71999998888",
                "email": "owner@test.com",
                "street": "Rua das Flores",
                "number": "120",
                "neighborhood": "Centro",
                "complement": "Apto 12",
            },
        )
        assert response.status_code == 200
        assert "Rua das Flores" in response.json()["primary_address_full"]

        get_response = api_client.get(f"{base_url}/api/owner-profile")
        assert get_response.status_code == 200
        assert get_response.json()["full_name"] == "TEST_Owner"

    def test_partner_application_flow(self, api_client, base_url):
        payload = self._build_partner_payload("1")
        create_response = api_client.post(f"{base_url}/api/partner-applications", json=payload)
        assert create_response.status_code == 201
        app_id = create_response.json()["id"]
        assert create_response.json()["status"] == "Em análise"

        list_response = api_client.get(f"{base_url}/api/partner-applications")
        assert list_response.status_code == 200
        assert any(item["id"] == app_id for item in list_response.json())

        approve_response = api_client.patch(
            f"{base_url}/api/partner-applications/{app_id}/status",
            json={"status": "Aprovado"},
        )
        assert approve_response.status_code == 200
        assert approve_response.json()["status"] == "Aprovado"

    def test_partner_application_requires_declaration(self, api_client, base_url):
        payload = self._build_partner_payload("2")
        payload["accepted_declaration"] = False
        response = api_client.post(f"{base_url}/api/partner-applications", json=payload)
        assert response.status_code == 400
        assert "Declaração" in response.json().get("detail", "")

    def test_photo_upload_updates_photo_url_and_is_fetchable(self, api_client, base_url):
        payload = self._build_payload("photo", datetime.now(timezone.utc) + timedelta(days=4))
        create_response = api_client.post(f"{base_url}/api/walks", json=payload)
        assert create_response.status_code == 201
        walk_id = create_response.json()["id"]

        file_name = f"test-{uuid.uuid4().hex}.png"
        png_header = b"\x89PNG\r\n\x1a\n"
        png_body = png_header + b"test-image-content"
        upload_response = api_client.post(
            f"{base_url}/api/walks/{walk_id}/photo",
            files={"file": (file_name, png_body, "image/png")},
        )
        assert upload_response.status_code == 200
        uploaded = upload_response.json()
        assert uploaded["photo_url"].startswith(f"/api/walks/{walk_id}/photo-file")

        get_response = api_client.get(f"{base_url}/api/walks/{walk_id}")
        assert get_response.status_code == 200
        fetched = get_response.json()
        assert fetched["photo_url"] == uploaded["photo_url"]

        photo_response = api_client.get(f"{base_url}{uploaded['photo_url']}")
        assert photo_response.status_code == 200

    def test_create_walk_with_invalid_datetime_returns_422(self, api_client, base_url):
        payload = {
            "pet_name": "TEST_Pet_invalid",
            "client_name": "TEST_Client_invalid",
            "walk_date": "2026-99-99",
            "walk_time": "25:61",
            "duration_minutes": 30,
            "walk_type": "Individual",
            "walker_id": "walker-1",
            "pickup_street": "Rua Teste",
            "pickup_number": "10",
            "pickup_neighborhood": "Centro",
            "pickup_complement": "",
            "location_reference": "Perto do mercado",
            "notes": "TEST_Invalid",
        }
        response = api_client.post(f"{base_url}/api/walks", json=payload)
        assert response.status_code == 422
        assert response.json().get("detail") == "Data ou horário inválido"

    def test_create_walk_with_invalid_walker_returns_400(self, api_client, base_url):
        payload = self._build_payload("invalid_walker", datetime.now(timezone.utc) + timedelta(days=2))
        payload["walker_id"] = "walker-does-not-exist"
        response = api_client.post(f"{base_url}/api/walks", json=payload)
        assert response.status_code == 400
        assert response.json().get("detail") == "Passeador inválido"

    def test_create_walk_without_pickup_street_returns_422(self, api_client, base_url):
        payload = self._build_payload("missing_street", datetime.now(timezone.utc) + timedelta(days=2))
        payload["pickup_street"] = ""
        response = api_client.post(f"{base_url}/api/walks", json=payload)
        assert response.status_code == 422

    def test_create_walk_without_pickup_number_returns_422(self, api_client, base_url):
        payload = self._build_payload("missing_number", datetime.now(timezone.utc) + timedelta(days=2))
        payload["pickup_number"] = ""
        response = api_client.post(f"{base_url}/api/walks", json=payload)
        assert response.status_code == 422

    def test_create_walk_without_pickup_neighborhood_returns_422(self, api_client, base_url):
        payload = self._build_payload("missing_neighborhood", datetime.now(timezone.utc) + timedelta(days=2))
        payload["pickup_neighborhood"] = ""
        response = api_client.post(f"{base_url}/api/walks", json=payload)
        assert response.status_code == 422

    def test_create_walk_without_walker_id_returns_422(self, api_client, base_url):
        payload = self._build_payload("missing_walker", datetime.now(timezone.utc) + timedelta(days=2))
        payload["walker_id"] = ""
        response = api_client.post(f"{base_url}/api/walks", json=payload)
        assert response.status_code == 422

    def test_upload_non_image_returns_400(self, api_client, base_url):
        payload = self._build_payload("nonimage", datetime.now(timezone.utc) + timedelta(days=5))
        create_response = api_client.post(f"{base_url}/api/walks", json=payload)
        assert create_response.status_code == 201
        walk_id = create_response.json()["id"]

        upload_response = api_client.post(
            f"{base_url}/api/walks/{walk_id}/photo",
            files={"file": ("not-image.txt", b"plain text", "text/plain")},
        )
        assert upload_response.status_code == 400
        assert upload_response.json().get("detail") == "Arquivo deve ser imagem"

    def test_shared_walk_blocks_reactive_pet(self, api_client, base_url):
        reactive_pet = api_client.post(f"{base_url}/api/pets", json=self._build_pet_payload("reactive", behavior="Reativo"))
        calm_pet = api_client.post(f"{base_url}/api/pets", json=self._build_pet_payload("calm"))
        assert reactive_pet.status_code == 201
        assert calm_pet.status_code == 201

        payload = self._build_shared_walk_payload(
            "shared_reactive",
            datetime.now(timezone.utc) + timedelta(days=1),
            reactive_pet.json()["id"],
            calm_pet.json()["id"],
        )
        response = api_client.post(f"{base_url}/api/walks", json=payload)
        assert response.status_code == 400
        assert "reativo" in response.json().get("detail", "").lower()

    def test_shared_walk_blocks_first_walk_pet(self, api_client, base_url):
        pet_a = api_client.post(f"{base_url}/api/pets", json=self._build_pet_payload("first_a"))
        pet_b = api_client.post(f"{base_url}/api/pets", json=self._build_pet_payload("first_b"))
        assert pet_a.status_code == 201
        assert pet_b.status_code == 201

        payload = self._build_shared_walk_payload(
            "shared_first",
            datetime.now(timezone.utc) + timedelta(days=1),
            pet_a.json()["id"],
            pet_b.json()["id"],
        )
        response = api_client.post(f"{base_url}/api/walks", json=payload)
        assert response.status_code == 400
        assert "finalizado" in response.json().get("detail", "").lower()

    def test_admin_can_approve_shared_walk_and_split_payments(self, api_client, base_url):
        pet_a = api_client.post(f"{base_url}/api/pets", json=self._build_pet_payload("approve_a"))

        client_session = requests.Session()
        client_login = client_session.post(
            f"{base_url}/api/auth/login",
            json={"email": "cliente@petpasso.com", "password": "Cliente@123"},
            timeout=20,
        )
        assert client_login.status_code == 200
        client_session.headers.update({"Authorization": f"Bearer {client_login.json()['access_token']}"})
        pet_b = client_session.post(f"{base_url}/api/pets", json=self._build_pet_payload("approve_b"), timeout=20)

        assert pet_a.status_code == 201
        assert pet_b.status_code == 201

        # cria passeios individuais finalizados para liberar regra de primeiro passeio
        for pet in [pet_a.json(), pet_b.json()]:
            single_payload = self._build_payload(f"history_{pet['id'][:4]}", datetime.now(timezone.utc) + timedelta(days=2))
            single_payload["pet_id"] = pet["id"]
            single_payload["pet_name"] = pet["pet_name"]
            single_payload["client_name"] = pet["owner_name"]
            create_history = api_client.post(f"{base_url}/api/walks", json=single_payload)
            assert create_history.status_code == 201
            walk_id = create_history.json()["id"]
            assert api_client.patch(f"{base_url}/api/walks/{walk_id}/status", json={"status": "Indo buscar o pet"}).status_code == 200
            assert api_client.patch(f"{base_url}/api/walks/{walk_id}/status", json={"status": "Passeando agora"}).status_code == 200
            assert api_client.patch(f"{base_url}/api/walks/{walk_id}/status", json={"status": "Finalizado"}).status_code == 200

        shared_payload = self._build_shared_walk_payload(
            "shared_pending",
            datetime.now(timezone.utc) + timedelta(days=3),
            pet_a.json()["id"],
            pet_a.json()["id"],
            shared_context="other_client",
        )
        shared_payload["second_pet_id"] = None
        shared_payload["pet_name"] = pet_a.json()["pet_name"]
        shared_payload["client_name"] = pet_a.json()["owner_name"]

        create_shared = api_client.post(f"{base_url}/api/walks", json=shared_payload)
        assert create_shared.status_code == 201
        shared_walk_id = create_shared.json()["id"]
        assert create_shared.json()["shared_approved"] is False

        for pet in [pet_a.json(), pet_b.json()]:
            eligibility = api_client.patch(
                f"{base_url}/api/admin/pets/{pet['id']}/shared-eligibility",
                json={"podeParticiparCompartilhado": True, "aprovadoParaCompartilhado": True},
            )
            assert eligibility.status_code == 200

        approve_shared = api_client.patch(
            f"{base_url}/api/admin/walks/{shared_walk_id}/shared-approval",
            json={"approved": True, "second_pet_id": pet_b.json()["id"], "walker_id": "walker-1"},
        )
        assert approve_shared.status_code == 200
        approved_walk = approve_shared.json()
        assert approved_walk["shared_approved"] is True
        assert len(approved_walk["pet_ids"]) == 2

        payments = api_client.get(f"{base_url}/api/admin/payments")
        assert payments.status_code == 200
        related = [item for item in payments.json() if item["walk_id"] == shared_walk_id]
        assert len(related) == 2
        assert all(item["value"] == 29.90 for item in related)

        # sem aprovação compartilhada não pode iniciar
        pending_shared_payload = self._build_shared_walk_payload(
            "shared_block",
            datetime.now(timezone.utc) + timedelta(days=4),
            pet_a.json()["id"],
            pet_a.json()["id"],
            shared_context="other_client",
        )
        pending_shared_payload["second_pet_id"] = None
        pending_shared_payload["pet_name"] = pet_a.json()["pet_name"]
        pending_shared_payload["client_name"] = pet_a.json()["owner_name"]

        pending_create = api_client.post(f"{base_url}/api/walks", json=pending_shared_payload)
        assert pending_create.status_code == 201
        pending_id = pending_create.json()["id"]
        start_pending = api_client.patch(
            f"{base_url}/api/walks/{pending_id}/status", json={"status": "Indo buscar o pet"}
        )
        assert start_pending.status_code == 400
