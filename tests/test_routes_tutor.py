"""Testes de ROTA (camada HTTP) do modulo app/routes/tutor.py.

Segue o padrao de tests/test_routes_onda1.py: monta um FastAPI MINIMO (NAO importa
app.main, que conecta no Neon), inclui SO o router de tutor, usa SQLite em memoria
com StaticPool + check_same_thread False e Base.metadata.create_all. Override de
get_db e get_current_user.

Cobre:
- GET /tutor/profile: 200 com None quando nao existe; 200 com perfil quando existe.
- POST /tutor/profile: happy path + normalizacao de cpf/phone (so digitos);
  upsert (POST com perfil existente atualiza); 400 cpf/phone invalido;
  409 cpf/phone duplicado de OUTRO usuario.
- PUT /tutor/profile: cria quando ausente; atualiza quando presente.
- 401 sem autenticacao (auth real, sem override de get_current_user).
"""
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  — registra todas as tabelas no Base.metadata
from app.core.database import Base, get_db
from app.dependencies.auth import get_current_user
from app.models.tenant import Tenant
from app.models.tutor_profile import TutorProfile
from app.models.user import User
from app.routes import tutor
from app.services.tenant_seed_service import DEFAULT_TENANT_SLUG

TENANT_ID = "t-test"
TUTOR_ID = "tutor-test"

VALID_CPF = "390.533.447-05"
VALID_CPF_DIGITS = "39053344705"
VALID_CPF_2 = "111.444.777-35"
VALID_CPF_2_DIGITS = "11144477735"
VALID_PHONE = "(71) 98888-7777"
VALID_PHONE_DIGITS = "71988887777"


def build(*, authed: bool = True):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    # slug = DEFAULT para resolve_current_tenant_id achar este tenant sem criar outro.
    db.add(Tenant(id=TENANT_ID, name="Aumigao", slug=DEFAULT_TENANT_SLUG, status="active", plan="business"))
    db.add(User(id=TUTOR_ID, email="tutor@test.com", password_hash="x", role="cliente", tenant_id=TENANT_ID))
    db.commit()

    test_app = FastAPI()
    test_app.include_router(tutor.router)
    test_app.dependency_overrides[get_db] = lambda: db
    if authed:
        test_app.dependency_overrides[get_current_user] = lambda: db.get(User, TUTOR_ID)
    return TestClient(test_app), db


def _payload(**overrides):
    base = {
        "full_name": "Joao Tutor",
        "cpf": VALID_CPF,
        "phone": VALID_PHONE,
        "cep": "40000-000",
        "street": "Rua A",
        "number": "10",
        "city": "Salvador",
        "state": "BA",
    }
    base.update(overrides)
    return base


# ----- GET profile -----
def test_get_profile_returns_null_when_absent():
    client, _ = build()
    r = client.get("/tutor/profile")
    assert r.status_code == 200, r.text
    assert r.json() is None


def test_get_profile_returns_existing():
    client, db = build()
    db.add(TutorProfile(id=str(uuid4()), user_id=TUTOR_ID, tenant_id=TENANT_ID,
                        full_name="Maria", cpf=VALID_CPF_DIGITS, phone=VALID_PHONE_DIGITS))
    db.commit()
    r = client.get("/tutor/profile")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["full_name"] == "Maria"
    assert body["cpf"] == VALID_CPF_DIGITS
    assert body["user_id"] == TUTOR_ID


def test_get_profile_backfills_null_tenant():
    # perfil legado sem tenant_id: GET deve preencher com o tenant resolvido.
    client, db = build()
    pid = str(uuid4())
    db.add(TutorProfile(id=pid, user_id=TUTOR_ID, tenant_id=None, full_name="Legado"))
    db.commit()
    r = client.get("/tutor/profile")
    assert r.status_code == 200, r.text
    db.expire_all()
    assert db.get(TutorProfile, pid).tenant_id == TENANT_ID


# ----- POST profile (create) -----
def test_create_profile_happy_path_and_normalization():
    client, db = build()
    r = client.post("/tutor/profile", json=_payload())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["full_name"] == "Joao Tutor"
    # normalizacao: so digitos persistidos
    assert body["cpf"] == VALID_CPF_DIGITS
    assert body["phone"] == VALID_PHONE_DIGITS
    assert body["id"]
    # persistido com tenant correto
    stored = db.query(TutorProfile).filter(TutorProfile.user_id == TUTOR_ID).first()
    assert stored is not None
    assert stored.tenant_id == TENANT_ID


def test_create_profile_invalid_cpf_returns_400():
    client, _ = build()
    r = client.post("/tutor/profile", json=_payload(cpf="123"))
    assert r.status_code == 400, r.text
    assert "CPF" in r.json()["detail"]


def test_create_profile_invalid_phone_returns_400():
    client, _ = build()
    r = client.post("/tutor/profile", json=_payload(phone="12"))
    assert r.status_code == 400, r.text
    assert "telefone" in r.json()["detail"].lower()


def test_create_profile_duplicate_cpf_returns_409():
    client, db = build()
    # outro usuario ja possui o CPF
    db.add(User(id="other", email="other@test.com", password_hash="x", role="cliente", tenant_id=TENANT_ID))
    db.add(TutorProfile(id=str(uuid4()), user_id="other", tenant_id=TENANT_ID, cpf=VALID_CPF_DIGITS))
    db.commit()
    r = client.post("/tutor/profile", json=_payload(cpf=VALID_CPF, phone=VALID_PHONE))
    assert r.status_code == 409, r.text
    assert "CPF" in r.json()["detail"]


def test_create_profile_duplicate_phone_returns_409():
    client, db = build()
    db.add(User(id="other", email="other@test.com", password_hash="x", role="cliente", tenant_id=TENANT_ID))
    db.add(TutorProfile(id=str(uuid4()), user_id="other", tenant_id=TENANT_ID, phone=VALID_PHONE_DIGITS))
    db.commit()
    # CPF diferente e valido, telefone duplicado
    r = client.post("/tutor/profile", json=_payload(cpf=VALID_CPF_2, phone=VALID_PHONE))
    assert r.status_code == 409, r.text
    assert "telefone" in r.json()["detail"].lower()


def test_post_profile_when_exists_acts_as_upsert():
    # POST com perfil ja existente delega para update_profile (atualiza, nao duplica).
    client, db = build()
    first = client.post("/tutor/profile", json=_payload(full_name="Original"))
    assert first.status_code == 200, first.text
    first_id = first.json()["id"]

    second = client.post("/tutor/profile", json=_payload(full_name="Atualizado"))
    assert second.status_code == 200, second.text
    assert second.json()["full_name"] == "Atualizado"
    assert second.json()["id"] == first_id  # mesmo perfil
    assert db.query(TutorProfile).filter(TutorProfile.user_id == TUTOR_ID).count() == 1


# ----- PUT profile -----
def test_put_profile_creates_when_absent():
    client, db = build()
    r = client.put("/tutor/profile", json=_payload(full_name="ViaPut"))
    assert r.status_code == 200, r.text
    assert r.json()["full_name"] == "ViaPut"
    assert r.json()["cpf"] == VALID_CPF_DIGITS
    assert db.query(TutorProfile).filter(TutorProfile.user_id == TUTOR_ID).count() == 1


def test_put_profile_updates_when_present():
    client, db = build()
    db.add(TutorProfile(id=str(uuid4()), user_id=TUTOR_ID, tenant_id=TENANT_ID, full_name="Antigo"))
    db.commit()
    r = client.put("/tutor/profile", json=_payload(full_name="Novo"))
    assert r.status_code == 200, r.text
    assert r.json()["full_name"] == "Novo"
    assert db.query(TutorProfile).filter(TutorProfile.user_id == TUTOR_ID).count() == 1


def test_put_profile_invalid_cpf_returns_400():
    client, _ = build()
    r = client.put("/tutor/profile", json=_payload(cpf="000.000.000-00"))
    assert r.status_code == 400, r.text


# ----- auth -----
def test_get_profile_requires_auth():
    client, _ = build(authed=False)
    r = client.get("/tutor/profile")
    assert r.status_code == 401, r.text


def test_create_profile_requires_auth():
    client, _ = build(authed=False)
    r = client.post("/tutor/profile", json=_payload())
    assert r.status_code == 401, r.text


def test_put_profile_requires_auth():
    client, _ = build(authed=False)
    r = client.put("/tutor/profile", json=_payload())
    assert r.status_code == 401, r.text
