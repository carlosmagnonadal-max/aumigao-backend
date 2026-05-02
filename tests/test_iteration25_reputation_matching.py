import uuid

import pytest
import requests


# Reputação e ranking de passeadores
def test_get_walkers_returns_reputation_ranking_fields(api_client, base_url):
    response = api_client.get(f"{base_url}/api/walkers", timeout=25)
    assert response.status_code == 200

    walkers = response.json()
    assert isinstance(walkers, list)
    assert len(walkers) > 0

    required_fields = {
        "public_rating_label",
        "public_badge",
        "score_final",
        "match_score",
        "quality_status",
        "is_top_match",
        "is_eligible_for_matching",
    }
    missing = required_fields - set(walkers[0].keys())
    assert not missing


def test_public_rating_rule_under_five_reviews_label(api_client, base_url):
    response = api_client.get(f"{base_url}/api/walkers", timeout=25)
    assert response.status_code == 200

    walkers = response.json()
    under_five = [w for w in walkers if int(w.get("rating_count", 0) or 0) < 5]
    assert under_five, "Esperado ao menos um passeador com menos de 5 avaliações"
    assert all(str(w.get("public_rating_label", "")).strip() == "Novo na plataforma" for w in under_five)


def test_restricted_or_suspended_never_eligible(api_client, base_url):
    response = api_client.get(f"{base_url}/api/walkers", timeout=25)
    assert response.status_code == 200

    walkers = response.json()
    for walker in walkers:
        if walker.get("quality_status") in {"restrito", "suspenso"}:
            assert walker.get("is_eligible_for_matching") is False


def test_top_match_is_not_preselected_server_side(api_client, base_url):
    response = api_client.get(f"{base_url}/api/walkers", timeout=25)
    assert response.status_code == 200

    walkers = response.json()
    top_matches = [w for w in walkers if bool(w.get("is_top_match"))]
    assert len(top_matches) <= 1


def test_walkers_list_is_ranked_by_match_score_desc(api_client, base_url):
    response = api_client.get(f"{base_url}/api/walkers", timeout=25)
    assert response.status_code == 200

    walkers = response.json()
    scores = [float(w.get("match_score", 0) or 0) for w in walkers]
    assert scores == sorted(scores, reverse=True)


def _walker_session(base_url: str) -> requests.Session:
    session = requests.Session()
    login = session.post(
        f"{base_url}/api/auth/login",
        json={"email": "passeador@petpasso.com", "password": "Passeador@123"},
        timeout=20,
    )
    assert login.status_code == 200
    token = login.json().get("access_token")
    assert token
    session.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/json"})
    return session


# Bloco de reputação do passeador
def test_walker_quality_endpoint_returns_reputation_block(base_url):
    session = _walker_session(base_url)
    try:
        response = session.get(f"{base_url}/api/walker/quality", timeout=25)
        assert response.status_code == 200
        payload = response.json()
        for field in ["score_final", "rating_recent_avg", "rating_weighted_avg", "score_trend", "encouragement_message"]:
            assert field in payload
    finally:
        session.close()


# Auth playbook: cookies/cors/lockout
def test_login_sets_http_only_cookies(base_url):
    response = requests.post(
        f"{base_url}/api/auth/login",
        json={"email": "cliente@petpasso.com", "password": "Cliente@123"},
        timeout=20,
    )
    assert response.status_code == 200
    set_cookie_header = response.headers.get("set-cookie", "")
    lowered = set_cookie_header.lower()
    assert "access_token=" in lowered
    assert "refresh_token=" in lowered
    assert "httponly" in lowered


def test_auth_me_works_with_cookie_session(base_url):
    session = requests.Session()
    login = session.post(
        f"{base_url}/api/auth/login",
        json={"email": "cliente@petpasso.com", "password": "Cliente@123"},
        timeout=20,
    )
    assert login.status_code == 200

    me = session.get(f"{base_url}/api/auth/me", timeout=20)
    assert me.status_code == 200
    assert me.json().get("email") == "cliente@petpasso.com"
    session.close()


def test_cors_preflight_allows_credentials_with_explicit_origin(base_url):
    origin = base_url.rstrip("/")
    response = requests.options(
        f"{base_url}/api/auth/login",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
        timeout=20,
    )
    assert response.status_code in (200, 204)
    assert response.headers.get("access-control-allow-credentials") == "true"
    assert response.headers.get("access-control-allow-origin") == origin


def test_bruteforce_lockout_after_five_failures(base_url):
    session = requests.Session()
    email = f"lockout_{uuid.uuid4().hex[:8]}@example.com"

    for _ in range(5):
        resp = session.post(
            f"{base_url}/api/auth/login",
            json={"email": email, "password": "senha-incorreta"},
            timeout=20,
        )
        assert resp.status_code == 401

    sixth = session.post(
        f"{base_url}/api/auth/login",
        json={"email": email, "password": "senha-incorreta"},
        timeout=20,
    )
    assert sixth.status_code == 429
