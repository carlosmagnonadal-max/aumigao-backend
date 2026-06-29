"""Testes de ROTA (camada HTTP) do modulo app/routes/legal.py.

Cobre o wiring real dos endpoints de termos legais / aceite LGPD:
- GET /legal/documents (publico, varia por role)
- GET /legal/acceptance (auth obrigatoria, reflete versao atual)
- POST /legal/acceptance (auth obrigatoria, 400 se nao aceitar)

Monta um FastAPI MINIMO so com o router de legal + overrides de get_db /
get_current_user (SQLite em memoria) - NAO importa app.main (que conecta no
banco de PROD). Padrao baseado em tests/test_routes_onda1.py.
"""
import app.models  # noqa: F401  - registra todas as tabelas no Base.metadata
from fastapi import FastAPI, HTTPException, status
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.dependencies.auth import get_current_user
from app.models.user import User
from app.routes import legal
from app.routes.legal import LEGAL_VERSION

TENANT_ID = "t-test"


def build(*, role: str = "tutor", authenticated: bool = True):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    user_id = f"user-{role}"
    db.add(User(id=user_id, email=f"{role}@test.com", password_hash="x", role=role, tenant_id=TENANT_ID))
    db.commit()

    test_app = FastAPI()
    test_app.include_router(legal.router)
    test_app.dependency_overrides[get_db] = lambda: db

    if authenticated:
        test_app.dependency_overrides[get_current_user] = lambda: db.get(User, user_id)
    else:
        def _unauth():
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Nao autenticado")

        test_app.dependency_overrides[get_current_user] = _unauth

    return TestClient(test_app), db, user_id


# ----- GET /legal/documents (publico) -----
def test_documents_default_is_tutor():
    client, _, _ = build()
    r = client.get("/legal/documents")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["role"] == "tutor"
    assert body["version"] == LEGAL_VERSION
    assert body["requires_acceptance"] is True
    types = {d["type"] for d in body["documents"]}
    assert {"terms", "privacy", "cancellation", "lgpd-consent", "geolocation-consent"} <= types
    assert all(d["audience"] == "Tutor" for d in body["documents"])
    assert all(d["version"] == LEGAL_VERSION for d in body["documents"])


def test_documents_role_passeador_via_query():
    client, _, _ = build()
    # endpoint usa o query param 'role' (sem usuario), nao o role do token
    r = client.get("/legal/documents", params={"role": "passeador"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["role"] == "passeador"
    assert all(d["audience"] == "Passeador" for d in body["documents"])
    titles = {d["title"] for d in body["documents"]}
    assert "Termos e Condicoes do Passeador" in titles or any("Passeador" in t for t in titles)


def test_documents_walker_alias_maps_to_passeador():
    client, _, _ = build()
    r = client.get("/legal/documents", params={"role": "walker"})
    assert r.status_code == 200, r.text
    assert r.json()["role"] == "passeador"


def test_documents_unknown_role_falls_back_to_tutor():
    client, _, _ = build()
    r = client.get("/legal/documents", params={"role": "qualquer-coisa"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["role"] == "tutor"
    assert all(d["audience"] == "Tutor" for d in body["documents"])


def test_documents_admin_role_preserved_but_uses_tutor_docs():
    # _normalize_role mantem 'admin'/'super_admin', mas _documents_for_role
    # cai no fallback de 'tutor' (sem entrada em LEGAL_DOCUMENTS_BY_ROLE).
    client, _, _ = build()
    r = client.get("/legal/documents", params={"role": "admin"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["role"] == "admin"
    assert body["documents"]  # nao vazio (fallback p/ tutor)
    assert all(d["audience"] == "Tutor" for d in body["documents"])


def test_documents_is_public_no_auth_required():
    # documents NAO depende de get_current_user; mesmo sem auth deve responder 200
    client, _, _ = build(authenticated=False)
    r = client.get("/legal/documents")
    assert r.status_code == 200, r.text


# ----- GET /legal/acceptance (auth obrigatoria) -----
def test_acceptance_status_requires_auth():
    client, _, _ = build(authenticated=False)
    r = client.get("/legal/acceptance")
    assert r.status_code == 401


def test_acceptance_status_not_accepted_initially():
    client, _, _ = build()
    r = client.get("/legal/acceptance")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["accepted"] is False
    assert body["acceptance"] is None
    assert body["version"] == LEGAL_VERSION
    assert body["versions"]["terms_version"] == LEGAL_VERSION


# ----- POST /legal/acceptance (auth obrigatoria) -----
def test_accept_requires_auth():
    client, _, _ = build(authenticated=False)
    r = client.post("/legal/acceptance", json={"role": "tutor", "accepted": True})
    assert r.status_code == 401


def test_accept_without_accepted_flag_returns_400():
    client, _, _ = build()
    r = client.post("/legal/acceptance", json={"role": "tutor", "accepted": False})
    assert r.status_code == 400
    assert "obrigatorio" in r.json()["detail"].lower()


def test_accept_default_payload_not_accepted_returns_400():
    # accepted tem default False no schema; corpo vazio = nao aceitou
    client, _, _ = build()
    r = client.post("/legal/acceptance", json={})
    assert r.status_code == 400


def test_accept_happy_path_then_status_current():
    client, _, _ = build()
    r = client.post("/legal/acceptance", json={"role": "tutor", "accepted": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["accepted"] is True
    assert body["role"] == "tutor"
    acc = body["acceptance"]
    assert acc is not None
    assert acc["terms_version"] == LEGAL_VERSION
    assert acc["lgpd_version"] == LEGAL_VERSION
    assert acc["geolocation_version"] == LEGAL_VERSION
    assert acc["user_role"] == "tutor"

    # GET agora reflete aceite atual
    status_body = client.get("/legal/acceptance").json()
    assert status_body["accepted"] is True
    assert status_body["acceptance"]["id"] == acc["id"]


def test_accept_passeador_role_persists_role():
    # role 'walker' explicito no payload -> normaliza para 'passeador'
    # (NOTE: o default do schema e 'tutor', entao corpo sem role NAO usa user.role)
    client, _, _ = build(role="walker")
    r = client.post("/legal/acceptance", json={"role": "walker", "accepted": True})
    assert r.status_code == 200, r.text
    assert r.json()["role"] == "passeador"
    # status para o mesmo role normalizado reflete aceite
    s = client.get("/legal/acceptance", params={"role": "passeador"}).json()
    assert s["accepted"] is True


def test_accept_for_one_role_does_not_satisfy_other_role():
    # aceite como tutor nao deve marcar passeador como aceito (filtro por user_role)
    client, _, _ = build(role="tutor")
    client.post("/legal/acceptance", json={"role": "tutor", "accepted": True})
    other = client.get("/legal/acceptance", params={"role": "passeador"}).json()
    assert other["accepted"] is False
    assert other["acceptance"] is None


# ----- Conteudo material que DEVE chegar ao usuario no aceite -----
def _terms_content(client, role: str) -> str:
    body = client.get("/legal/documents", params={"role": role}).json()
    terms = next(d for d in body["documents"] if d["type"] == "terms")
    return terms["content"].lower()


def test_tutor_terms_disclose_credit_expiration():
    # #8: a expiracao de credito (breakage) so e oponivel se divulgada no aceite (CDC art. 54).
    client, _, _ = build()
    content = _terms_content(client, "tutor")
    assert "crédito" in content
    assert "expiram" in content
    assert "art. 49" in content  # direito de arrependimento
    assert "não são" in content and "dinheiro" in content  # nao conversivel em dinheiro


def test_passeador_terms_reinforce_autonomy():
    # #5: reforco anti-subordinacao precisa estar no texto in-app do aceite.
    client, _, _ = build(role="passeador")
    content = _terms_content(client, "passeador")
    assert "não é penalizado por recusar" in content
    assert "mei" in content  # microempreendedor individual
    assert "por passeio efetivamente realizado" in content
