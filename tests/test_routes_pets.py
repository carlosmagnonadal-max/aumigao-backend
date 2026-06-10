"""Testes de ROTA (camada HTTP) do modulo app/routes/pets.py.

Monta um FastAPI MINIMO com SO o router de pets + overrides de get_db /
get_current_user (SQLite em memoria, StaticPool) — NAO importa app.main
(que conecta no banco de PROD). Cobre CRUD do tutor logado, ownership,
validacoes e auth (401 sem credenciais).

Observacao sobre "ownership": a rota filtra por Pet.tutor_id == user.id, logo
um pet de OUTRO tutor nao e encontrado e retorna 404 (e nao 403). Testamos o
comportamento REAL (404) e documentamos isso em bug_or_gap.
"""
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  — registra todas as tabelas no Base.metadata
from app.core.database import Base, get_db
from app.dependencies.auth import get_current_user
from app.models.pet import Pet
from app.models.tenant import Tenant
from app.models.user import User
from app.routes import pets
from app.services.tenant_seed_service import DEFAULT_TENANT_SLUG

TENANT_ID = "t-test"
TUTOR_ID = "tutor-test"
OTHER_ID = "other-test"


def build(*, authed: bool = True, pets_for=None):
    """Monta app + dados. pets_for: dict {pet_id: tutor_id} de pets pre-existentes."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    db.add(Tenant(id=TENANT_ID, name="Aumigao", slug=DEFAULT_TENANT_SLUG, status="active", plan="business"))
    db.add(User(id=TUTOR_ID, email="tutor@test.com", password_hash="x", role="cliente", tenant_id=TENANT_ID))
    db.add(User(id=OTHER_ID, email="other@test.com", password_hash="x", role="cliente", tenant_id=TENANT_ID))
    for pid, owner in (pets_for or {}).items():
        db.add(Pet(id=pid, tutor_id=owner, tenant_id=TENANT_ID, name=pid))
    db.commit()

    test_app = FastAPI()
    test_app.include_router(pets.router)
    test_app.dependency_overrides[get_db] = lambda: db
    if authed:
        test_app.dependency_overrides[get_current_user] = lambda: db.get(User, TUTOR_ID)
    # se authed=False NAO sobrescrevemos get_current_user -> roda o real (HTTPBearer)
    return TestClient(test_app), db


# ---------------- happy path: CRUD ----------------
def test_create_pet_happy_path():
    client, db = build()
    r = client.post("/pets", json={"name": "Rex", "species": "Cachorro", "size": "M"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "Rex"
    assert body["tutor_id"] == TUTOR_ID
    assert body["id"]
    # tenant_id resolvido e gravado
    pet = db.get(Pet, body["id"])
    assert pet.tenant_id == TENANT_ID


def test_list_pets_only_own():
    client, _ = build(pets_for={"meu": TUTOR_ID, "alheio": OTHER_ID})
    r = client.get("/pets")
    assert r.status_code == 200
    ids = {p["id"] for p in r.json()}
    assert ids == {"meu"}  # nao vaza pet do outro tutor


def test_list_pets_sets_is_neutered_default():
    client, _ = build(pets_for={"meu": TUTOR_ID})
    r = client.get("/pets")
    assert r.status_code == 200
    assert r.json()[0]["is_neutered"] is False


def test_get_pet_happy_path():
    client, _ = build(pets_for={"meu": TUTOR_ID})
    r = client.get("/pets/meu")
    assert r.status_code == 200
    assert r.json()["id"] == "meu"


def test_update_pet_happy_path():
    client, db = build(pets_for={"meu": TUTOR_ID})
    r = client.put("/pets/meu", json={"name": "Novo Nome", "size": "G"})
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "Novo Nome"
    assert r.json()["size"] == "G"
    assert db.get(Pet, "meu").name == "Novo Nome"


def test_update_pet_partial_keeps_other_fields():
    client, _ = build(pets_for={"meu": TUTOR_ID})
    # so muda size; name nao deve ser apagado (exclude_unset)
    r = client.put("/pets/meu", json={"size": "P"})
    assert r.status_code == 200, r.text
    assert r.json()["size"] == "P"
    assert r.json()["name"] == "meu"  # nome original preservado


def test_delete_pet_happy_path():
    client, db = build(pets_for={"meu": TUTOR_ID})
    r = client.delete("/pets/meu")
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert db.get(Pet, "meu") is None


# ---------------- ownership ----------------
def test_get_other_tutor_pet_returns_404():
    # rota filtra por tutor_id; pet de outro NAO e encontrado -> 404
    client, _ = build(pets_for={"alheio": OTHER_ID})
    r = client.get("/pets/alheio")
    assert r.status_code == 404


def test_update_other_tutor_pet_returns_404():
    client, _ = build(pets_for={"alheio": OTHER_ID})
    r = client.put("/pets/alheio", json={"name": "Hack"})
    assert r.status_code == 404


def test_delete_other_tutor_pet_returns_404():
    client, db = build(pets_for={"alheio": OTHER_ID})
    r = client.delete("/pets/alheio")
    assert r.status_code == 404
    assert db.get(Pet, "alheio") is not None  # pet do outro intacto


def test_get_nonexistent_pet_returns_404():
    client, _ = build()
    assert client.get("/pets/nope").status_code == 404


# ---------------- validacoes ----------------
def test_create_pet_without_name_returns_422():
    client, _ = build()
    r = client.post("/pets", json={"species": "Cachorro"})  # name obrigatorio
    assert r.status_code == 422


def test_create_pet_normalizes_local_photo_url():
    # file:/blob:/data:image viram None (normalizacao)
    client, _ = build()
    r = client.post("/pets", json={"name": "Rex", "photo_url": "file:///tmp/x.jpg"})
    assert r.status_code == 200, r.text
    assert r.json()["photo_url"] is None


def test_create_pet_keeps_http_photo_url():
    client, _ = build()
    url = "https://cdn.example.com/pet.jpg"
    r = client.post("/pets", json={"name": "Rex", "photo_url": url})
    assert r.status_code == 200, r.text
    assert r.json()["photo_url"] == url


# ---------------- auth (401 sem credenciais) ----------------
def test_list_pets_requires_auth():
    client, _ = build(authed=False)
    assert client.get("/pets").status_code == 401


def test_create_pet_requires_auth():
    client, _ = build(authed=False)
    r = client.post("/pets", json={"name": "Rex"})
    assert r.status_code == 401


def test_get_pet_requires_auth():
    client, _ = build(authed=False, pets_for={"meu": TUTOR_ID})
    assert client.get("/pets/meu").status_code == 401


def test_delete_pet_requires_auth():
    client, _ = build(authed=False, pets_for={"meu": TUTOR_ID})
    assert client.delete("/pets/meu").status_code == 401
