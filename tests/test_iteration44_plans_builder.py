import os
from pathlib import Path

import pytest
import requests
from dotenv import dotenv_values
from pymongo import MongoClient


def _resolve_base_url() -> str:
    env_url = os.environ.get("EXPO_BACKEND_URL")
    if env_url:
        return env_url.rstrip("/")

    frontend_env = Path("/app/frontend/.env")
    if frontend_env.exists():
        values = dotenv_values(frontend_env)
        env_url = values.get("EXPO_BACKEND_URL") or values.get("EXPO_PUBLIC_BACKEND_URL")
        if env_url:
            return str(env_url).rstrip("/")

    raise RuntimeError("EXPO_BACKEND_URL is not configured")


BASE_URL = _resolve_base_url()


def _mongo_database():
    backend_env = Path("/app/backend/.env")
    values = dotenv_values(backend_env) if backend_env.exists() else {}
    mongo_url = os.environ.get("MONGO_URL") or values.get("MONGO_URL")
    db_name = os.environ.get("DB_NAME") or values.get("DB_NAME")
    if not mongo_url or not db_name:
        pytest.skip("Mongo settings unavailable for persistence check")
    client = MongoClient(str(mongo_url).strip().strip('"'))
    return client, client[str(db_name).strip().strip('"')]


@pytest.fixture()
def client_session():
    """Auth setup for plans endpoints (cliente role)."""
    session = requests.Session()
    session.headers.update({"Accept": "application/json", "Content-Type": "application/json"})

    login = session.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": "cliente@petpasso.com", "password": "Cliente@123"},
        timeout=30,
    )
    assert login.status_code == 200, f"Login falhou: {login.status_code} {login.text}"
    login_data = login.json()
    token = login_data.get("access_token")
    assert token, "Login sem access_token"
    session.headers.update({"Authorization": f"Bearer {token}"})

    yield session
    session.close()


def _simulate(session: requests.Session, freq: int, plan: str, minutes: int):
    response = session.post(
        f"{BASE_URL}/api/plans/simulate",
        json={
            "frequencia_semanal": freq,
            "duracao_plano": plan,
            "duracao_passeio": minutes,
        },
        timeout=30,
    )
    assert response.status_code == 200, f"Simulação falhou: {response.status_code} {response.text}"
    return response.json()


# Plans simulation matrix and dynamic calculations
def test_total_walks_by_plan_duration(client_session):
    mensal = _simulate(client_session, 2, "mensal", 45)
    trimestral = _simulate(client_session, 2, "trimestral", 45)
    semestral = _simulate(client_session, 2, "semestral", 45)

    assert mensal["total_passeios"] == 8
    assert trimestral["total_passeios"] == 24
    assert semestral["total_passeios"] == 48


def test_frequency_discount_mapping_with_margin_guard_behavior(client_session):
    one = _simulate(client_session, 1, "mensal", 45)
    two = _simulate(client_session, 2, "mensal", 45)
    three = _simulate(client_session, 3, "mensal", 45)
    four = _simulate(client_session, 4, "mensal", 45)
    five = _simulate(client_session, 5, "mensal", 45)

    assert one["desconto_frequencia_percent"] == 0.0
    assert two["desconto_frequencia_percent"] == 3.0
    assert three["desconto_frequencia_percent"] == 5.0
    assert four["desconto_reduzido_por_margem"] is True
    assert four["desconto_total_percent"] == 5.0
    assert five["desconto_reduzido_por_margem"] is True
    assert five["desconto_total_percent"] == 5.0


def test_plan_discount_mapping_without_reduction(client_session):
    mensal = _simulate(client_session, 1, "mensal", 45)
    trimestral = _simulate(client_session, 1, "trimestral", 45)
    semestral = _simulate(client_session, 1, "semestral", 45)

    assert mensal["desconto_plano_percent"] == 0.0
    assert trimestral["desconto_plano_percent"] == 3.0
    assert semestral["desconto_reduzido_por_margem"] is True
    assert semestral["desconto_total_percent"] == 5.0


def test_discount_cap_never_exceeds_15_percent(client_session):
    data = _simulate(client_session, 5, "semestral", 60)
    assert data["desconto_total_percent"] <= 15.0


def test_margin_minimum_protection_applies_and_preserves_floor(client_session):
    data = _simulate(client_session, 5, "semestral", 45)

    assert data["desconto_reduzido_por_margem"] is True
    assert data["margem_minima_percent"] == 15.0
    assert data["margem_estimada_percent"] >= 15.0


def test_financial_rounding_two_decimals(client_session):
    data = _simulate(client_session, 2, "trimestral", 45)

    for key in ["valor_total_sem_desconto", "valor_total_com_desconto", "valor_por_passeio", "economia"]:
        value = float(data[key])
        assert round(value, 2) == value


def test_comparison_and_savings_message_fields(client_session):
    data = _simulate(client_session, 3, "mensal", 30)

    assert isinstance(data["comparacao_avulso_total"], float)
    assert isinstance(data["comparacao_avulso_por_passeio"], float)
    assert "Você economiza" in data["mensagem_economia"]


def test_ready_for_subscription_payload_structure(client_session):
    data = _simulate(client_session, 2, "trimestral", 60)

    assert data["ready_for_subscription"] is True
    assert isinstance(data["subscription_payload"], dict)
    assert data["subscription_payload"]["ready_for_subscription"] is True
    assert data["subscription_payload"]["duracao_plano"] == "trimestral"


# Subscription intent endpoint behavior
def test_subscription_intent_returns_pending_provider_integration(client_session):
    response = client_session.post(
        f"{BASE_URL}/api/plans/subscription-intent",
        json={
            "frequencia_semanal": 3,
            "duracao_plano": "mensal",
            "duracao_passeio": 45,
        },
        timeout=30,
    )
    assert response.status_code == 200, f"Intent falhou: {response.status_code} {response.text}"
    data = response.json()

    assert data["status"] == "pending_provider_integration"
    assert data["ready_for_subscription"] is True
    assert data["intent_id"]
    assert data["summary"]["ready_for_subscription"] is True


def test_subscription_intent_persists_in_database(client_session):
    response = client_session.post(
        f"{BASE_URL}/api/plans/subscription-intent",
        json={
            "frequencia_semanal": 2,
            "duracao_plano": "trimestral",
            "duracao_passeio": 30,
        },
        timeout=30,
    )
    assert response.status_code == 200, f"Intent falhou: {response.status_code} {response.text}"
    data = response.json()

    mongo_client, db = _mongo_database()
    try:
        row = db.plan_subscription_intents.find_one({"id": data["intent_id"]}, {"_id": 0})
        assert row is not None
        assert row["status"] == "pending_provider_integration"
        assert row["ready_for_subscription"] is True
        assert row["payload"]["duracao_plano"] == "trimestral"
    finally:
        mongo_client.close()
