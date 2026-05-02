from datetime import datetime, timezone


# Module coverage: internal admin candidate review, activation and walker visibility rules
class TestAdminWalkerManagement:
    def _candidate_payload(self, suffix: str):
        stamp = datetime.now(timezone.utc).strftime("%H%M%S%f")
        return {
            "full_name": f"TEST_AdminCandidate_{suffix}_{stamp}",
            "phone": "71990002222",
            "email": f"test_admin_{suffix}_{stamp}@test.com",
            "neighborhood_region": "Pituba",
            "has_pet_experience": True,
            "has_third_party_experience": True,
            "experience_description": "TEST experiência admin para gestão interna.",
            "availability": "Segunda a sexta, 8h às 18h",
            "profile_photo_url": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB",
            "accepted_declaration": True,
        }

    def _create_candidate(self, api_client, base_url, suffix: str):
        response = api_client.post(
            f"{base_url}/api/partner-applications",
            json=self._candidate_payload(suffix),
        )
        assert response.status_code == 201
        return response.json()

    def test_candidate_admin_fields_exist_and_persist(self, api_client, base_url):
        created = self._create_candidate(api_client, base_url, "fields")
        candidate_id = created["id"]

        assert created["status"] == "Em análise"
        assert created["created_at"]
        assert "approved_at" not in created
        assert "internal_notes" not in created
        assert "active_as_walker" not in created

        admin_view = api_client.get(f"{base_url}/api/admin/partner-applications/{candidate_id}")
        assert admin_view.status_code == 200
        assert admin_view.json()["approved_at"] is None
        assert admin_view.json()["internal_notes"] == ""
        assert admin_view.json()["active_as_walker"] is False

        notes_response = api_client.patch(
            f"{base_url}/api/partner-applications/{candidate_id}/admin-fields",
            json={"internal_notes": "TEST observação interna"},
        )
        assert notes_response.status_code == 200
        assert notes_response.json()["internal_notes"] == "TEST observação interna"

        get_response = api_client.get(f"{base_url}/api/partner-applications/{candidate_id}")
        assert get_response.status_code == 200
        assert "internal_notes" not in get_response.json()

        admin_detail = api_client.get(f"{base_url}/api/admin/partner-applications/{candidate_id}")
        assert admin_detail.status_code == 200
        assert admin_detail.json()["internal_notes"] == "TEST observação interna"

    def test_activation_only_after_approval_and_walkers_visibility(self, api_client, base_url):
        created = self._create_candidate(api_client, base_url, "activation")
        candidate_id = created["id"]

        before_approval = api_client.patch(
            f"{base_url}/api/partner-applications/{candidate_id}/admin-fields",
            json={"active_as_walker": True},
        )
        assert before_approval.status_code == 400

        approve_response = api_client.patch(
            f"{base_url}/api/partner-applications/{candidate_id}/status",
            json={"status": "Aprovado"},
        )
        assert approve_response.status_code == 200
        assert approve_response.json()["approved_at"] is not None

        activate_response = api_client.patch(
            f"{base_url}/api/partner-applications/{candidate_id}/admin-fields",
            json={"active_as_walker": True},
        )
        assert activate_response.status_code == 200
        assert activate_response.json()["active_as_walker"] is True

        walkers_response = api_client.get(f"{base_url}/api/walkers")
        assert walkers_response.status_code == 200
        walkers = walkers_response.json()
        assert any(w["id"] == f"partner-{candidate_id}" for w in walkers)

        move_back_to_review = api_client.patch(
            f"{base_url}/api/partner-applications/{candidate_id}/status",
            json={"status": "Em análise"},
        )
        assert move_back_to_review.status_code == 200
        assert move_back_to_review.json()["active_as_walker"] is False
        assert move_back_to_review.json()["approved_at"] is None

        walkers_after = api_client.get(f"{base_url}/api/walkers")
        assert walkers_after.status_code == 200
        assert all(w["id"] != f"partner-{candidate_id}" for w in walkers_after.json())

    def test_internal_notes_should_not_be_exposed_in_candidate_list_payload(self, api_client, base_url):
        created = self._create_candidate(api_client, base_url, "privacy")
        candidate_id = created["id"]

        notes_response = api_client.patch(
            f"{base_url}/api/partner-applications/{candidate_id}/admin-fields",
            json={"internal_notes": "TEST segredo interno"},
        )
        assert notes_response.status_code == 200

        list_response = api_client.get(f"{base_url}/api/partner-applications")
        assert list_response.status_code == 200

        listed_candidate = next(item for item in list_response.json() if item["id"] == candidate_id)
        assert "internal_notes" not in listed_candidate