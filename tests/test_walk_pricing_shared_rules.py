import uuid
from datetime import datetime, timedelta, timezone

import requests


# Module coverage: walk type rules, pet compatibility rules, dynamic pricing, and payment persistence.
class TestWalkPricingAndSharedRules:
    def _future_dt(self, days: int = 1):
        candidate = datetime.now(timezone.utc) + timedelta(days=days)
        while candidate.weekday() >= 5:
            candidate += timedelta(days=1)
        target_hour = 10 + (days % 5)
        return candidate.replace(hour=target_hour, minute=0, second=0, microsecond=0)

    def _pet_payload(self, tag: str, accepts_shared_walk: bool = True):
        return {
            "pet_name": f"TEST_PricingPet_{tag}_{uuid.uuid4().hex[:6]}",
            "behavioral_notes": "TEST comportamento",
            "photo_url": "",
            "owner_name": "",
            "gets_along_with_dogs": True,
            "accepts_shared_walk": accepts_shared_walk,
            "pet_size": "Médio",
            "energy_level": "Médio",
            "pulls_leash": False,
            "dog_behavior": "Neutro",
        }

    def _walk_payload(
        self,
        tag: str,
        dt: datetime,
        *,
        pet_id: str,
        walk_type: str = "Individual",
        duration_minutes: int = 30,
        second_pet_id: str | None = None,
    ):
        payload = {
            "pet_name": f"TEST_WalkPet_{tag}",
            "pet_id": pet_id,
            "client_name": f"TEST_Client_{tag}",
            "walk_date": dt.strftime("%Y-%m-%d"),
            "walk_time": dt.strftime("%H:%M"),
            "duration_minutes": duration_minutes,
            "walker_id": "walker-1",
            "pickup_street": "Rua Teste",
            "pickup_number": "123",
            "pickup_neighborhood": "Centro",
            "pickup_complement": "",
            "location_reference": "Praça",
            "notes": "TEST walkthrough",
            "walk_type": walk_type,
        }
        if second_pet_id:
            payload["second_pet_id"] = second_pet_id
        return payload

    def _next_available_slot(self, api_client, base_url: str, *, duration_minutes: int, used_slots: set[tuple[str, str]]):
        for day_offset in range(1, 25):
            date_value = (datetime.now(timezone.utc) + timedelta(days=day_offset)).strftime("%Y-%m-%d")
            response = api_client.get(
                f"{base_url}/api/walkers/walker-1/availability-slots",
                params={"date": date_value, "duration_minutes": duration_minutes},
            )
            if response.status_code != 200:
                continue
            slots = response.json().get("available_slots", [])
            for time_value in slots:
                slot_key = (date_value, time_value)
                if slot_key in used_slots:
                    continue
                used_slots.add(slot_key)
                return slot_key
        raise AssertionError("Nenhum horário disponível encontrado para o teste")

    def _finish_walk(self, api_client, base_url: str, walk_id: str):
        assert api_client.patch(f"{base_url}/api/walks/{walk_id}/status", json={"status": "Indo buscar o pet"}).status_code == 200
        assert api_client.patch(f"{base_url}/api/walks/{walk_id}/status", json={"status": "Passeando agora"}).status_code == 200
        assert api_client.patch(f"{base_url}/api/walks/{walk_id}/status", json={"status": "Finalizado"}).status_code == 200

    def _create_client_session(self, base_url: str):
        session = requests.Session()
        login = session.post(
            f"{base_url}/api/auth/login",
            json={"email": "cliente@petpasso.com", "password": "Cliente@123"},
            timeout=20,
        )
        assert login.status_code == 200
        token = login.json()["access_token"]
        session.headers.update({"Authorization": f"Bearer {token}"})
        return session

    def _create_coupon(
        self,
        api_client,
        base_url: str,
        *,
        code: str,
        discount_percent: float | None = None,
        discount_fixed: float | None = None,
        max_uses_per_user: int = 1,
        applicable_walk_types: list[str] | None = None,
    ):
        payload = {
            "code": code,
            "discount_percent": discount_percent,
            "discount_fixed": discount_fixed,
            "max_uses_per_user": max_uses_per_user,
            "max_global_uses": 100,
            "applicable_walk_types": applicable_walk_types or ["Individual", "Compartilhado"],
            "is_active": True,
        }
        response = api_client.post(f"{base_url}/api/admin/coupons", json=payload)
        assert response.status_code == 201
        return response.json()

    def test_walk_type_is_required(self, api_client, base_url):
        pet = api_client.post(f"{base_url}/api/pets", json=self._pet_payload("required_type"))
        assert pet.status_code == 201

        payload = self._walk_payload(
            "required_type",
            self._future_dt(),
            pet_id=pet.json()["id"],
            walk_type="Individual",
        )
        payload.pop("walk_type")

        response = api_client.post(f"{base_url}/api/walks", json=payload)
        assert response.status_code == 422

    def test_individual_pricing_table_30_45_60(self, api_client, base_url):
        pet = api_client.post(f"{base_url}/api/pets", json=self._pet_payload("individual_price"))
        assert pet.status_code == 201
        pet_id = pet.json()["id"]

        expected_by_duration = {30: 29.90, 45: 34.90, 60: 39.90}
        for duration, expected in expected_by_duration.items():
            payload = self._walk_payload(
                f"individual_{duration}",
                self._future_dt(days=duration // 15),
                pet_id=pet_id,
                walk_type="Individual",
                duration_minutes=duration,
            )
            created = api_client.post(f"{base_url}/api/walks", json=payload)
            assert created.status_code == 201
            walk = created.json()
            assert walk["walk_type"] == "Individual"
            assert walk["base_price"] == expected

            payments = api_client.get(f"{base_url}/api/admin/payments")
            assert payments.status_code == 200
            related = [p for p in payments.json() if p["walk_id"] == walk["id"]]
            assert len(related) == 1
            assert related[0]["value"] == expected

    def test_individual_blocks_second_pet_rule(self, api_client, base_url):
        pet_a = api_client.post(f"{base_url}/api/pets", json=self._pet_payload("individual_a"))
        pet_b = api_client.post(f"{base_url}/api/pets", json=self._pet_payload("individual_b"))
        assert pet_a.status_code == 201
        assert pet_b.status_code == 201

        payload = self._walk_payload(
            "individual_second_pet",
            self._future_dt(),
            pet_id=pet_a.json()["id"],
            walk_type="Individual",
            duration_minutes=30,
            second_pet_id=pet_b.json()["id"],
        )

        response = api_client.post(f"{base_url}/api/walks", json=payload)
        assert response.status_code == 400

    def test_shared_blocks_pet_with_accepts_shared_false(self, api_client, base_url):
        pet_blocked = api_client.post(
            f"{base_url}/api/pets",
            json=self._pet_payload("blocked_shared", accepts_shared_walk=False),
        )
        pet_ok = api_client.post(f"{base_url}/api/pets", json=self._pet_payload("ok_shared"))
        assert pet_blocked.status_code == 201
        assert pet_ok.status_code == 201

        payload = self._walk_payload(
            "shared_blocked",
            self._future_dt(),
            pet_id=pet_blocked.json()["id"],
            walk_type="Compartilhado",
            duration_minutes=45,
            second_pet_id=pet_ok.json()["id"],
        )

        response = api_client.post(f"{base_url}/api/walks", json=payload)
        assert response.status_code == 400
        assert "não aceita passeio compartilhado" in response.json().get("detail", "").lower()

    def test_shared_same_tutor_two_pets_price_5490(self, api_client, base_url):
        pet_a = api_client.post(f"{base_url}/api/pets", json=self._pet_payload("same_tutor_a"))
        pet_b = api_client.post(f"{base_url}/api/pets", json=self._pet_payload("same_tutor_b"))
        assert pet_a.status_code == 201
        assert pet_b.status_code == 201

        used_slots: set[tuple[str, str]] = set()
        for index, pet in enumerate([pet_a.json(), pet_b.json()]):
            payload_hist = self._walk_payload(
                f"history_same_{pet['id'][:4]}",
                self._future_dt(days=2 + index),
                pet_id=pet["id"],
                walk_type="Individual",
                duration_minutes=30,
            )
            slot_date, slot_time = self._next_available_slot(api_client, base_url, duration_minutes=30, used_slots=used_slots)
            payload_hist["walk_date"] = slot_date
            payload_hist["walk_time"] = slot_time
            hist = api_client.post(f"{base_url}/api/walks", json=payload_hist)
            assert hist.status_code == 201
            self._finish_walk(api_client, base_url, hist.json()["id"])

        payload = self._walk_payload(
            "same_tutor_shared",
            self._future_dt(days=3),
            pet_id=pet_a.json()["id"],
            walk_type="Compartilhado",
            duration_minutes=45,
            second_pet_id=pet_b.json()["id"],
        )
        shared_date, shared_time = self._next_available_slot(api_client, base_url, duration_minutes=45, used_slots=used_slots)
        payload["walk_date"] = shared_date
        payload["walk_time"] = shared_time
        created = api_client.post(f"{base_url}/api/walks", json=payload)
        assert created.status_code == 201
        walk = created.json()
        assert walk["base_price"] == 54.90

        payments = api_client.get(f"{base_url}/api/admin/payments")
        assert payments.status_code == 200
        related = [p for p in payments.json() if p["walk_id"] == walk["id"]]
        assert len(related) == 1
        assert related[0]["value"] == 54.90

    def test_shared_two_tutors_price_2990_per_pet_and_split_payments(self, api_client, base_url):
        admin_pet = api_client.post(f"{base_url}/api/pets", json=self._pet_payload("admin_tutor"))
        assert admin_pet.status_code == 201

        client_session = self._create_client_session(base_url)
        client_pet = client_session.post(f"{base_url}/api/pets", json=self._pet_payload("client_tutor"), timeout=20)
        assert client_pet.status_code == 201

        used_slots: set[tuple[str, str]] = set()
        for index, pet in enumerate([admin_pet.json(), client_pet.json()]):
            payload_hist = self._walk_payload(
                f"history_cross_{pet['id'][:4]}",
                self._future_dt(days=2 + index),
                pet_id=pet["id"],
                walk_type="Individual",
                duration_minutes=30,
            )
            slot_date, slot_time = self._next_available_slot(api_client, base_url, duration_minutes=30, used_slots=used_slots)
            payload_hist["walk_date"] = slot_date
            payload_hist["walk_time"] = slot_time
            hist = api_client.post(f"{base_url}/api/walks", json=payload_hist)
            assert hist.status_code == 201
            self._finish_walk(api_client, base_url, hist.json()["id"])

        payload = self._walk_payload(
            "two_tutors_shared",
            self._future_dt(days=3),
            pet_id=admin_pet.json()["id"],
            walk_type="Compartilhado",
            duration_minutes=45,
            second_pet_id=client_pet.json()["id"],
        )
        shared_date, shared_time = self._next_available_slot(api_client, base_url, duration_minutes=45, used_slots=used_slots)
        payload["walk_date"] = shared_date
        payload["walk_time"] = shared_time
        created = api_client.post(f"{base_url}/api/walks", json=payload)
        assert created.status_code == 201
        walk = created.json()
        assert walk["shared_context"] == "other_client"
        assert walk["base_price"] == 59.80

        payments = api_client.get(f"{base_url}/api/admin/payments")
        assert payments.status_code == 200
        related = [p for p in payments.json() if p["walk_id"] == walk["id"]]
        assert len(related) == 2
        assert all(item["value"] == 29.90 for item in related)

    def test_coupon_percent_and_fixed_apply_together(self, api_client, base_url):
        pet = api_client.post(f"{base_url}/api/pets", json=self._pet_payload("coupon_both"))
        assert pet.status_code == 201

        code = f"BOTH{uuid.uuid4().hex[:6]}".upper()
        self._create_coupon(
            api_client,
            base_url,
            code=code,
            discount_percent=10,
            discount_fixed=5,
            max_uses_per_user=2,
            applicable_walk_types=["Individual", "Compartilhado"],
        )

        payload = self._walk_payload(
            "coupon_both",
            self._future_dt(days=5),
            pet_id=pet.json()["id"],
            walk_type="Individual",
            duration_minutes=30,
        )
        payload["coupon_code"] = code

        created = api_client.post(f"{base_url}/api/walks", json=payload)
        assert created.status_code == 201
        walk = created.json()
        assert walk["coupon_code"] == code
        assert walk["discount_amount"] == 7.99
        assert walk["base_price"] == 21.91

        payments = api_client.get(f"{base_url}/api/admin/payments")
        assert payments.status_code == 200
        related = [p for p in payments.json() if p["walk_id"] == walk["id"]]
        assert len(related) == 1
        assert related[0]["value"] == 21.91

    def test_coupon_usage_limit_per_user_is_enforced(self, api_client, base_url):
        pet = api_client.post(f"{base_url}/api/pets", json=self._pet_payload("coupon_limit"))
        assert pet.status_code == 201

        code = f"LIMIT{uuid.uuid4().hex[:6]}".upper()
        self._create_coupon(
            api_client,
            base_url,
            code=code,
            discount_percent=15,
            discount_fixed=0,
            max_uses_per_user=1,
            applicable_walk_types=["Individual", "Compartilhado"],
        )

        first_payload = self._walk_payload(
            "coupon_limit_first",
            self._future_dt(days=6),
            pet_id=pet.json()["id"],
            walk_type="Individual",
            duration_minutes=30,
        )
        first_payload["coupon_code"] = code

        first_created = api_client.post(f"{base_url}/api/walks", json=first_payload)
        assert first_created.status_code == 201

        second_payload = self._walk_payload(
            "coupon_limit_second",
            self._future_dt(days=7),
            pet_id=pet.json()["id"],
            walk_type="Individual",
            duration_minutes=30,
        )
        second_payload["coupon_code"] = code

        second_created = api_client.post(f"{base_url}/api/walks", json=second_payload)
        assert second_created.status_code == 400
        assert "limite de uso" in second_created.json().get("detail", "").lower()
