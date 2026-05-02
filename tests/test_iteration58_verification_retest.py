from datetime import date, timedelta

import pytest
import requests


# Module coverage: iter58 retest for verification flag masking, low-score guard, and ranking ordering.


def _login(base_url: str, email: str, password: str) -> requests.Session:
    session = requests.Session()
    response = session.post(
        f"{base_url}/api/auth/login",
        json={"email": email, "password": password},
        timeout=35,
    )
    if response.status_code != 200:
        session.close()
        pytest.skip(f"Login indisponível para {email}: {response.status_code}")
    token = (response.json() or {}).get("access_token")
    if not token:
        session.close()
        pytest.skip(f"Token ausente para {email}")
    session.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/json"})
    return session


def _find_date_with_walkers(client: requests.Session, base_url: str) -> tuple[str, list[dict]]:
    for offset in range(1, 15):
        target_date = (date.today() + timedelta(days=offset)).isoformat()
        response = client.get(
            f"{base_url}/api/walkers",
            params={"date": target_date, "duration_minutes": 30, "tipo_passeio": "padrao"},
            timeout=35,
        )
        if response.status_code != 200:
            continue
        rows = response.json() if isinstance(response.json(), list) else []
        if rows:
            return target_date, rows
    pytest.skip("Sem passeadores disponíveis para validação")


@pytest.fixture()
def iter58_scope(base_url: str):
    admin = _login(base_url, "superadmin@petpasso.com", "SuperAdmin@123")
    client = _login(base_url, "cliente@petpasso.com", "Cliente@123")

    flags_resp = admin.get(f"{base_url}/api/admin/feature-flags", timeout=35)
    assert flags_resp.status_code == 200, flags_resp.text
    baseline_flags = {row["feature_name"]: row for row in flags_resp.json()}

    yield {"base_url": base_url, "admin": admin, "client": client, "baseline_flags": baseline_flags}

    original = baseline_flags.get("walker_verification_enabled")
    if original:
        admin.patch(
            f"{base_url}/api/admin/feature-flags/walker_verification_enabled",
            json={"is_active": bool(original.get("is_active", False)), "is_visible": bool(original.get("is_visible", False))},
            timeout=35,
        )
    admin.close()
    client.close()


def test_fix1_flag_off_masks_verification_levels(iter58_scope):
    base_url = iter58_scope["base_url"]
    admin = iter58_scope["admin"]
    client = iter58_scope["client"]

    disable_resp = admin.patch(
        f"{base_url}/api/admin/feature-flags/walker_verification_enabled",
        json={"is_active": False, "is_visible": False},
        timeout=35,
    )
    assert disable_resp.status_code == 200, disable_resp.text

    _, walkers = _find_date_with_walkers(client, base_url)
    for row in walkers:
        assert str(row.get("verification_level") or "NONE") == "NONE"
        assert row.get("is_verified") is False
        assert float(row.get("verification_boost_points") or 0.0) == 0.0
        assert float(row.get("verification_priority_bonus_points") or 0.0) == 0.0


def test_fix4_no_boost_for_plus_premium_when_score_base_below_55(iter58_scope):
    base_url = iter58_scope["base_url"]
    admin = iter58_scope["admin"]
    client = iter58_scope["client"]

    enable_resp = admin.patch(
        f"{base_url}/api/admin/feature-flags/walker_verification_enabled",
        json={"is_active": True, "is_visible": True},
        timeout=35,
    )
    assert enable_resp.status_code == 200, enable_resp.text

    _, walkers = _find_date_with_walkers(client, base_url)
    candidates = [
        row
        for row in walkers
        if str(row.get("verification_level") or "NONE") in {"PLUS", "PREMIUM"}
        and float(row.get("score_base_component") or 0.0) < 55.0
    ]
    if not candidates:
        pytest.skip("Sem passeadores PLUS/PREMIUM com score_base_component < 55 no dataset atual")

    for row in candidates:
        assert float(row.get("verification_boost_points") or 0.0) == 0.0
        assert float(row.get("verification_priority_bonus_points") or 0.0) == 0.0


def test_fix4_top_candidate_not_below_55_when_eligible_alternative_exists(iter58_scope):
    base_url = iter58_scope["base_url"]
    client = iter58_scope["client"]
    _, walkers = _find_date_with_walkers(client, base_url)

    eligible = [row for row in walkers if bool(row.get("is_eligible_for_matching"))]
    if len(eligible) < 2:
        pytest.skip("Poucos candidatos elegíveis para validar proteção de score_base")

    top = eligible[0]
    top_base = float(top.get("score_base_component") or 0.0)
    has_safer_alternative = any(float(row.get("score_base_component") or 0.0) >= 55.0 for row in eligible[1:])
    if top_base < 55.0 and has_safer_alternative:
        pytest.fail("Top elegível com score_base_component < 55 apesar de alternativa >=55 disponível")


def test_fix5_ranking_order_respects_priority_quality_and_proximity(iter58_scope):
    base_url = iter58_scope["base_url"]
    client = iter58_scope["client"]
    _, walkers = _find_date_with_walkers(client, base_url)

    observed_ids = [str(row.get("id") or "") for row in walkers]
    expected = sorted(
        walkers,
        key=lambda row: (
            0 if row.get("is_eligible_for_matching") else 1,
            0 if row.get("within_primary_radius", False) else 1,
            -float(row.get("ranking_score_final") or row.get("match_score") or 0.0),
            -float(row.get("verification_priority_bonus_points") or 0.0),
            float(row.get("distance_proxy_km") or 999.0),
            -float(row.get("score_confiabilidade_component") or 0.0),
            -float(row.get("score_base_component") or 0.0),
        ),
    )
    expected_ids = [str(row.get("id") or "") for row in expected]
    assert observed_ids == expected_ids
