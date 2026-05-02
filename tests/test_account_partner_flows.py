from datetime import datetime, timezone


# Module coverage: owner profile persistence and partner application review workflow
class TestAccountPartnerFlows:
    def _owner_payload(self, suffix: str):
        return {
            "full_name": f"TEST_Owner_{suffix}",
            "phone": "71999990001",
            "email": f"owner_{suffix}@test.com",
            "street": "Rua das Laranjeiras",
            "number": "55",
            "neighborhood": "Centro",
            "complement": "Casa A",
        }

    def _partner_payload(self, suffix: str):
        now_stamp = datetime.now(timezone.utc).strftime("%H%M%S")
        return {
            "full_name": f"TEST_Partner_{suffix}_{now_stamp}",
            "phone": "71999990002",
            "email": f"partner_{suffix}_{now_stamp}@test.com",
            "neighborhood_region": "Pituba",
            "has_pet_experience": True,
            "has_third_party_experience": False,
            "experience_description": "TEST experiência com passeios e cuidados diários.",
            "availability": "Segunda a sexta das 8h às 17h",
            "profile_photo_url": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB",
            "accepted_declaration": True,
        }

    def test_owner_profile_create_and_get_persistence(self, api_client, base_url):
        payload = self._owner_payload("create")
        put_response = api_client.put(f"{base_url}/api/owner-profile", json=payload)
        assert put_response.status_code == 200
        saved = put_response.json()
        assert saved["full_name"] == payload["full_name"]
        assert saved["street"] == payload["street"]
        assert "Rua das Laranjeiras" in saved["primary_address_full"]

        get_response = api_client.get(f"{base_url}/api/owner-profile")
        assert get_response.status_code == 200
        fetched = get_response.json()
        assert fetched["full_name"] == payload["full_name"]
        assert fetched["number"] == payload["number"]

    def test_owner_profile_edit_persists_latest_values(self, api_client, base_url):
        initial = self._owner_payload("edit_a")
        updated = self._owner_payload("edit_b")
        updated["street"] = "Avenida Atualizada"
        updated["number"] = "999"

        create_response = api_client.put(f"{base_url}/api/owner-profile", json=initial)
        assert create_response.status_code == 200

        update_response = api_client.put(f"{base_url}/api/owner-profile", json=updated)
        assert update_response.status_code == 200
        assert update_response.json()["street"] == "Avenida Atualizada"

        get_response = api_client.get(f"{base_url}/api/owner-profile")
        assert get_response.status_code == 200
        fetched = get_response.json()
        assert fetched["street"] == "Avenida Atualizada"
        assert fetched["number"] == "999"

    def test_partner_application_created_with_default_status_em_analise(self, api_client, base_url):
        payload = self._partner_payload("status_default")
        create_response = api_client.post(f"{base_url}/api/partner-applications", json=payload)
        assert create_response.status_code == 201
        created = create_response.json()
        assert created["status"] == "Em análise"
        assert created["accepted_declaration"] is True

        admin_list = api_client.get(f"{base_url}/api/admin/partner-applications")
        assert admin_list.status_code == 200
        admin_created = next(item for item in admin_list.json() if item["id"] == created["id"])
        assert admin_created["active_as_walker"] is False

        list_response = api_client.get(f"{base_url}/api/partner-applications")
        assert list_response.status_code == 200
        listed = list_response.json()
        target = next(item for item in listed if item["id"] == created["id"])
        assert target["status"] == "Em análise"

    def test_partner_application_requires_declaration_checkbox(self, api_client, base_url):
        payload = self._partner_payload("declaration")
        payload["accepted_declaration"] = False
        response = api_client.post(f"{base_url}/api/partner-applications", json=payload)
        assert response.status_code == 400
        assert "Declaração" in response.json().get("detail", "")

    def test_partner_review_status_update_flow(self, api_client, base_url):
        payload = self._partner_payload("review")
        create_response = api_client.post(f"{base_url}/api/partner-applications", json=payload)
        assert create_response.status_code == 201
        application_id = create_response.json()["id"]

        approve_response = api_client.patch(
            f"{base_url}/api/partner-applications/{application_id}/status",
            json={"status": "Aprovado"},
        )
        assert approve_response.status_code == 200
        assert approve_response.json()["status"] == "Aprovado"
        assert approve_response.json()["active_as_walker"] is False
        assert approve_response.json()["approved_at"] is not None

        reject_response = api_client.patch(
            f"{base_url}/api/partner-applications/{application_id}/status",
            json={"status": "Reprovado"},
        )
        assert reject_response.status_code == 200
        assert reject_response.json()["status"] == "Reprovado"
        assert reject_response.json()["active_as_walker"] is False

    def test_partner_activation_requires_approval_and_appears_in_walkers(self, api_client, base_url):
        payload = self._partner_payload("activate")
        create_response = api_client.post(f"{base_url}/api/partner-applications", json=payload)
        assert create_response.status_code == 201
        app_id = create_response.json()["id"]

        activate_before_approval = api_client.patch(
            f"{base_url}/api/partner-applications/{app_id}/admin-fields",
            json={"active_as_walker": True},
        )
        assert activate_before_approval.status_code == 400

        _ = api_client.patch(
            f"{base_url}/api/partner-applications/{app_id}/status",
            json={"status": "Aprovado"},
        )

        activate_after_approval = api_client.patch(
            f"{base_url}/api/partner-applications/{app_id}/admin-fields",
            json={"active_as_walker": True, "internal_notes": "TEST pronto para começar"},
        )
        assert activate_after_approval.status_code == 200
        assert activate_after_approval.json()["active_as_walker"] is True

        walkers = api_client.get(f"{base_url}/api/walkers")
        assert walkers.status_code == 200
        assert any(item["id"] == f"partner-{app_id}" for item in walkers.json())

    def test_owner_and_partner_flows_do_not_mix(self, api_client, base_url):
        before_list = api_client.get(f"{base_url}/api/partner-applications")
        assert before_list.status_code == 200
        count_before = len(before_list.json())

        owner_response = api_client.put(f"{base_url}/api/owner-profile", json=self._owner_payload("isolation"))
        assert owner_response.status_code == 200

        after_list = api_client.get(f"{base_url}/api/partner-applications")
        assert after_list.status_code == 200
        assert len(after_list.json()) == count_before

    def test_admin_business_endpoints_for_clients_walks_and_payments(self, api_client, base_url):
        walk_payload = {
            "pet_name": "TEST_AdminPet",
            "client_name": "TEST_AdminClient",
            "walk_date": "2026-05-10",
            "walk_time": "09:00",
            "duration_minutes": 45,
            "walk_type": "Individual",
            "walker_id": "walker-1",
            "pickup_street": "Rua A",
            "pickup_number": "10",
            "pickup_neighborhood": "Centro",
            "pickup_complement": "",
            "location_reference": "Centro",
            "pet_behavior_notes": "",
            "notes": "TEST_Note",
        }
        create_walk = api_client.post(f"{base_url}/api/walks", json=walk_payload)
        assert create_walk.status_code == 201
        walk_id = create_walk.json()["id"]

        dashboard = api_client.get(f"{base_url}/api/admin/dashboard")
        assert dashboard.status_code == 200
        assert "total_clients" in dashboard.json()

        clients = api_client.get(f"{base_url}/api/admin/clients")
        assert clients.status_code == 200
        assert any(item["name"] == "TEST_AdminClient" for item in clients.json())

        client_id = next(item["id"] for item in clients.json() if item["name"] == "TEST_AdminClient")
        client_detail = api_client.get(f"{base_url}/api/admin/clients/{client_id}")
        assert client_detail.status_code == 200
        assert client_detail.json()["name"] == "TEST_AdminClient"

        admin_walks = api_client.get(f"{base_url}/api/admin/walks")
        assert admin_walks.status_code == 200
        assert any(item["id"] == walk_id for item in admin_walks.json())

        cancel_walk = api_client.patch(
            f"{base_url}/api/admin/walks/{walk_id}/status", json={"status": "Cancelado"}
        )
        assert cancel_walk.status_code == 200
        assert cancel_walk.json()["status"] == "Cancelado"

        payments = api_client.get(f"{base_url}/api/admin/payments")
        assert payments.status_code == 200
        payment = next(item for item in payments.json() if item["walk_id"] == walk_id)

        payment_update = api_client.patch(
            f"{base_url}/api/admin/payments/{payment['id']}/status",
            json={"payment_status": "Pago", "payment_method": "Pix", "notes": "TEST_Payment"},
        )
        assert payment_update.status_code == 200
        assert payment_update.json()["payment_status"] == "Pago"
