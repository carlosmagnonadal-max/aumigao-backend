"""R11 — cobertura de asaas_subscription_service (antes: 0 testes).

Sem rede: httpx.AsyncClient é substituído por um fake; coroutines rodam via
asyncio.run (não exige pytest-asyncio). Cobre: modo não-configurado (None),
mapeamento interval→cycle, externalReference sub:{id}, sucesso, e erros 502.
"""
import asyncio

import pytest
from fastapi import HTTPException

import app.services.asaas_subscription_service as svc


class _FakeResp:
    def __init__(self, status_code, json_data=None):
        self.status_code = status_code
        self._json = json_data or {}
        self.text = str(json_data)

    def json(self):
        return self._json


class _FakeClient:
    """Imita httpx.AsyncClient como async context manager, sem rede."""
    def __init__(self, resp, capture):
        self._resp = resp
        self._capture = capture

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None):
        self._capture["url"] = url
        self._capture["payload"] = json
        return self._resp

    async def delete(self, url):
        self._capture["url"] = url
        return self._resp


def _patch(monkeypatch, *, cfg, resp):
    capture = {}
    monkeypatch.setattr(svc, "_get_config", lambda: cfg)
    if resp is not None:
        monkeypatch.setattr(svc.httpx, "AsyncClient", lambda **kw: _FakeClient(resp, capture))
    return capture


_CFG = {"api_key": "k", "base_url": "https://sandbox.asaas", "is_live": False}


# --------------------------------------------------------------- create
def test_create_returns_none_when_unconfigured(monkeypatch):
    _patch(monkeypatch, cfg=None, resp=None)
    out = asyncio.run(svc.create_asaas_subscription(
        customer_id="c1", value=10.0, interval="monthly", tutor_subscription_id="ts-1"))
    assert out is None


def test_create_returns_none_when_no_api_key(monkeypatch):
    _patch(monkeypatch, cfg={"api_key": "", "base_url": "x", "is_live": False}, resp=None)
    out = asyncio.run(svc.create_asaas_subscription(
        customer_id="c1", value=10.0, interval="monthly", tutor_subscription_id="ts-1"))
    assert out is None


def test_create_success_maps_cycle_and_external_ref(monkeypatch):
    cap = _patch(monkeypatch, cfg=_CFG, resp=_FakeResp(200, {"id": "asaas-sub-1"}))
    out = asyncio.run(svc.create_asaas_subscription(
        customer_id="c1", value=49.9, interval="monthly", tutor_subscription_id="ts-1"))
    assert out == "asaas-sub-1"
    assert cap["payload"]["cycle"] == "MONTHLY"
    assert cap["payload"]["externalReference"] == "sub:ts-1"
    assert cap["payload"]["billingType"] == "UNDEFINED"  # sandbox


def test_create_weekly_maps_weekly_cycle(monkeypatch):
    cap = _patch(monkeypatch, cfg=_CFG, resp=_FakeResp(200, {"id": "s"}))
    asyncio.run(svc.create_asaas_subscription(
        customer_id="c1", value=10.0, interval="weekly", tutor_subscription_id="ts-2"))
    assert cap["payload"]["cycle"] == "WEEKLY"


def test_create_unknown_interval_defaults_monthly(monkeypatch):
    cap = _patch(monkeypatch, cfg=_CFG, resp=_FakeResp(200, {"id": "s"}))
    asyncio.run(svc.create_asaas_subscription(
        customer_id="c1", value=10.0, interval="zzz", tutor_subscription_id="ts-3"))
    assert cap["payload"]["cycle"] == svc.DEFAULT_CYCLE == "MONTHLY"


def test_create_live_uses_undefined_billing(monkeypatch):
    # A-01: em live o billingType e UNDEFINED (tutor escolhe PIX/cartao/boleto na fatura).
    cfg_live = {"api_key": "k", "base_url": "https://api.asaas", "is_live": True}
    cap = _patch(monkeypatch, cfg=cfg_live, resp=_FakeResp(200, {"id": "s"}))
    asyncio.run(svc.create_asaas_subscription(
        customer_id="c1", value=10.0, interval="monthly", tutor_subscription_id="ts-4"))
    assert cap["payload"]["billingType"] == "UNDEFINED"


def test_create_raises_502_on_gateway_error(monkeypatch):
    _patch(monkeypatch, cfg=_CFG, resp=_FakeResp(400, {"errors": [{"description": "saldo"}]}))
    with pytest.raises(HTTPException) as ei:
        asyncio.run(svc.create_asaas_subscription(
            customer_id="c1", value=10.0, interval="monthly", tutor_subscription_id="ts-5"))
    assert ei.value.status_code == 502
    assert "saldo" in ei.value.detail


# --------------------------------------------------------------- cancel
def test_cancel_noop_when_unconfigured(monkeypatch):
    _patch(monkeypatch, cfg=None, resp=None)
    assert asyncio.run(svc.cancel_asaas_subscription("sub-1")) is None


def test_cancel_noop_when_no_id(monkeypatch):
    _patch(monkeypatch, cfg=_CFG, resp=None)
    assert asyncio.run(svc.cancel_asaas_subscription("")) is None


def test_cancel_ignores_404(monkeypatch):
    _patch(monkeypatch, cfg=_CFG, resp=_FakeResp(404))
    # já inexistente: não levanta
    assert asyncio.run(svc.cancel_asaas_subscription("sub-x")) is None


def test_cancel_raises_502_on_error(monkeypatch):
    _patch(monkeypatch, cfg=_CFG, resp=_FakeResp(500, {"description": "falhou"}))
    with pytest.raises(HTTPException) as ei:
        asyncio.run(svc.cancel_asaas_subscription("sub-y"))
    assert ei.value.status_code == 502
