"""Testes de rota para POST /client-errors.

Monta um FastAPI mínimo só com o router de client_errors (sem importar app.main,
que conecta no banco de prod). Padrão estabelecido em tests/test_routes_legal.py.

Cobre:
- Payload de error válido → 204 + log em ERROR (caplog).
- Payload de warn válido → 204 + log em WARNING.
- Mensagem acima de max_length → 422.
- Campo obrigatório ausente → 422.
"""
import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routes import client_errors
from app.routes.client_errors import _rate_limiter


@pytest.fixture(autouse=True)
def reset_rate_limiter():
    """Garante que o rate limiter começa zerado em cada teste."""
    _rate_limiter._failures.clear()
    yield
    _rate_limiter._failures.clear()


@pytest.fixture()
def client():
    test_app = FastAPI()
    test_app.include_router(client_errors.router)
    return TestClient(test_app)


# ---------------------------------------------------------------------------
# Casos válidos
# ---------------------------------------------------------------------------

def test_valid_error_returns_204_and_logs_at_error(client, caplog):
    payload = {
        "level": "error",
        "message": "Crash ao abrir tela de passeios",
        "error_type": "TypeError",
        "stack": "TypeError: Cannot read property 'id' of undefined\n  at WalksScreen:42",
        "platform": "ios",
        "app_version": "1.2.3",
        "context": {"screen": "WalksScreen", "action": "loadWalks"},
    }
    with caplog.at_level(logging.ERROR, logger="app.client_errors"):
        resp = client.post("/client-errors", json=payload)

    assert resp.status_code == 204
    assert resp.content == b""

    # Deve ter gerado pelo menos um registro de ERROR no logger correto.
    error_records = [
        r for r in caplog.records
        if r.name == "app.client_errors" and r.levelno == logging.ERROR
    ]
    assert error_records, "Esperava ao menos um log de ERROR em app.client_errors"
    assert "TypeError" in error_records[0].message
    assert "Crash ao abrir tela de passeios" in error_records[0].message


def test_valid_warn_returns_204_and_logs_at_warning(client, caplog):
    payload = {
        "level": "warn",
        "message": "Token de refresh próximo do vencimento",
        "platform": "android",
        "app_version": "1.2.3",
    }
    with caplog.at_level(logging.WARNING, logger="app.client_errors"):
        resp = client.post("/client-errors", json=payload)

    assert resp.status_code == 204

    warning_records = [
        r for r in caplog.records
        if r.name == "app.client_errors" and r.levelno == logging.WARNING
    ]
    assert warning_records, "Esperava ao menos um log de WARNING em app.client_errors"


def test_minimal_payload_returns_204(client, caplog):
    """Apenas os campos obrigatórios (level + message)."""
    payload = {"level": "error", "message": "Erro mínimo"}
    with caplog.at_level(logging.ERROR, logger="app.client_errors"):
        resp = client.post("/client-errors", json=payload)
    assert resp.status_code == 204


# ---------------------------------------------------------------------------
# Validação de entrada — 422
# ---------------------------------------------------------------------------

def test_message_exceeds_max_length_returns_422(client):
    payload = {
        "level": "error",
        "message": "x" * 2001,  # 1 char acima do limite de 2000
    }
    resp = client.post("/client-errors", json=payload)
    assert resp.status_code == 422


def test_missing_required_level_returns_422(client):
    payload = {"message": "Sem level"}
    resp = client.post("/client-errors", json=payload)
    assert resp.status_code == 422


def test_missing_required_message_returns_422(client):
    payload = {"level": "error"}
    resp = client.post("/client-errors", json=payload)
    assert resp.status_code == 422


def test_invalid_level_value_returns_422(client):
    payload = {"level": "critical", "message": "Level inválido"}
    resp = client.post("/client-errors", json=payload)
    assert resp.status_code == 422


def test_stack_exceeds_max_length_returns_422(client):
    payload = {
        "level": "error",
        "message": "Stack enorme",
        "stack": "x" * 8001,
    }
    resp = client.post("/client-errors", json=payload)
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Rate limit — 429
# ---------------------------------------------------------------------------

def test_rate_limit_returns_429_after_threshold(client):
    """Exceder o limite de chamadas por IP deve retornar 429."""
    # Seta o limite baixo para o teste não precisar fazer 60 chamadas.
    original_max = _rate_limiter.max_failures
    _rate_limiter.max_failures = 3
    try:
        payload = {"level": "error", "message": "spam"}
        for _ in range(3):
            client.post("/client-errors", json=payload)
        # A próxima deve ser bloqueada.
        resp = client.post("/client-errors", json=payload)
        assert resp.status_code == 429
    finally:
        _rate_limiter.max_failures = original_max
