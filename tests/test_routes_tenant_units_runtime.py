"""Testes de ROTA (camada HTTP) do modulo app/routes/tenant_units_runtime.py.

Padrao do projeto (ver tests/test_routes_onda1.py e tests/test_routes_auth.py):
monta um FastAPI MINIMO com apenas o router deste modulo, SQLite em memoria
(StaticPool), override de get_db. NAO importa app.main (que conecta no Neon).

NOTA sobre o FOCO: o briefing menciona "limites por plano (can_add_tenant_unit),
auth", mas o modulo-alvo tenant_units_runtime.py NAO tem dependencia de auth nem
logica de limite de plano. Ele e um endpoint de RUNTIME publico, somente-leitura,
que expoe as unidades (filiais) de um tenant para o app/site. A logica de
can_add_tenant_unit vive em app/routes/tenants.py (outro modulo). Estes testes
cobrem o comportamento REAL do alvo: resolucao de tenant (por id, por slug, e
default), serializacao (slug derivado do name, enabled = status == 'active'),
ordenacao por created_at e o endpoint /current.
"""
from datetime import datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 - registra todas as tabelas no Base.metadata
from app.core.database import Base, get_db
from app.models.tenant import Tenant, TenantUnit
from app.routes import tenant_units_runtime
from app.services.tenant_seed_service import DEFAULT_TENANT_SLUG

TENANT_ID = "t-test"
OTHER_TENANT_ID = "t-other"


def build(*, units: list[dict] | None = None, extra_tenants: list[dict] | None = None):
    """Monta app minimo com os routers do modulo-alvo e SQLite em memoria isolado."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    # slug = DEFAULT para get_default_tenant resolver este tenant sem criar outro.
    db.add(Tenant(id=TENANT_ID, name="Aumigao", slug=DEFAULT_TENANT_SLUG, status="active", plan="business"))
    for t in extra_tenants or []:
        db.add(Tenant(**t))
    for u in units or []:
        db.add(TenantUnit(**u))
    db.commit()

    test_app = FastAPI()
    test_app.include_router(tenant_units_runtime.router)
    test_app.include_router(tenant_units_runtime.api_router)
    test_app.dependency_overrides[get_db] = lambda: db
    return TestClient(test_app), db


def make_unit(uid, tenant_id=TENANT_ID, name="Unidade Centro", status="active", created_at=None):
    return dict(
        id=uid,
        tenant_id=tenant_id,
        name=name,
        status=status,
        created_at=created_at or datetime(2026, 1, 1),
    )


# --------------------------------------------------- GET /{tenant_id} por id ---
def test_units_runtime_by_tenant_id_happy_path():
    client, _ = build(units=[make_unit("u1", name="Unidade Centro")])
    r = client.get(f"/tenants/{TENANT_ID}/units-runtime")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tenant_id"] == TENANT_ID
    assert len(body["units"]) == 1
    unit = body["units"][0]
    assert unit["id"] == "u1"
    assert unit["name"] == "Unidade Centro"
    assert unit["slug"] == "unidade-centro"  # slug derivado do name
    assert unit["enabled"] is True  # status == "active"


def test_units_runtime_empty_when_no_units():
    client, _ = build(units=[])
    r = client.get(f"/tenants/{TENANT_ID}/units-runtime")
    assert r.status_code == 200
    body = r.json()
    assert body["tenant_id"] == TENANT_ID
    assert body["units"] == []


def test_inactive_unit_is_not_enabled():
    client, _ = build(units=[make_unit("u1", status="inactive", name="Filial Norte")])
    body = client.get(f"/tenants/{TENANT_ID}/units-runtime").json()
    assert body["units"][0]["enabled"] is False
    assert body["units"][0]["slug"] == "filial-norte"


def test_slug_normalizes_accents_and_symbols():
    client, _ = build(units=[make_unit("u1", name="Unidade São Paulo / Zona Sul")])
    body = client.get(f"/tenants/{TENANT_ID}/units-runtime").json()
    # NFKD remove acentos, simbolos viram hifen, sem hifen duplicado/nas pontas
    assert body["units"][0]["slug"] == "unidade-sao-paulo-zona-sul"


def test_units_ordered_by_created_at_asc():
    base = datetime(2026, 1, 1)
    client, _ = build(units=[
        make_unit("u-late", name="Tardia", created_at=base + timedelta(days=10)),
        make_unit("u-early", name="Antiga", created_at=base),
        make_unit("u-mid", name="Meio", created_at=base + timedelta(days=5)),
    ])
    body = client.get(f"/tenants/{TENANT_ID}/units-runtime").json()
    ids = [u["id"] for u in body["units"]]
    assert ids == ["u-early", "u-mid", "u-late"]


def test_units_runtime_filters_by_tenant():
    # unidade de OUTRO tenant nao deve aparecer
    client, _ = build(
        extra_tenants=[dict(id=OTHER_TENANT_ID, name="Outro", slug="outro", status="active", plan="basic")],
        units=[
            make_unit("u-mine", tenant_id=TENANT_ID, name="Minha"),
            make_unit("u-theirs", tenant_id=OTHER_TENANT_ID, name="Deles"),
        ],
    )
    body = client.get(f"/tenants/{TENANT_ID}/units-runtime").json()
    ids = [u["id"] for u in body["units"]]
    assert ids == ["u-mine"]


# ------------------------------------------------ GET /{tenant_id} por slug ---
def test_units_runtime_resolves_tenant_by_slug():
    # passar o slug do tenant (nao o id) tambem resolve
    client, _ = build(units=[make_unit("u1", name="Centro")])
    r = client.get(f"/tenants/{DEFAULT_TENANT_SLUG}/units-runtime")
    assert r.status_code == 200
    body = r.json()
    assert body["tenant_id"] == TENANT_ID
    assert body["units"][0]["id"] == "u1"


def test_units_runtime_unknown_tenant_falls_back_to_default():
    # tenant_id inexistente -> _resolve_tenant cai no default tenant
    client, _ = build(units=[make_unit("u1", name="Centro")])
    r = client.get("/tenants/nao-existe-xyz/units-runtime")
    assert r.status_code == 200
    body = r.json()
    assert body["tenant_id"] == TENANT_ID  # caiu no default
    assert body["units"][0]["id"] == "u1"


# ----------------------------------------------------- GET /current ----------
def test_current_units_runtime_resolves_default_tenant():
    client, _ = build(units=[make_unit("u1", name="Centro")])
    r = client.get("/tenants/current/units-runtime")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tenant_id"] == TENANT_ID
    assert body["units"][0]["id"] == "u1"


# ----------------------------------------------------- /api prefix -----------
def test_api_prefixed_route_works():
    client, _ = build(units=[make_unit("u1", name="Centro")])
    r = client.get(f"/api/tenants/{TENANT_ID}/units-runtime")
    assert r.status_code == 200, r.text
    assert r.json()["units"][0]["id"] == "u1"


def test_api_prefixed_current_route_works():
    client, _ = build(units=[make_unit("u1", name="Centro")])
    r = client.get("/api/tenants/current/units-runtime")
    assert r.status_code == 200
    assert r.json()["tenant_id"] == TENANT_ID
