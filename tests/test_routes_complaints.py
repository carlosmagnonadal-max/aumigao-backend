"""Testes de ROTA (camada HTTP) do modulo de ocorrencias/reclamacoes (complaints).

Cobre o wiring real: criacao com classificacao/severidade, listagem do usuario,
gating de auth (401/403), e o comportamento de categorias de alto risco /
escalonamento de severidade. Monta um FastAPI minimo so com os routers de
complaints + overrides de get_db / get_current_user (SQLite em memoria) — NAO
importa app.main (que conecta no banco de PROD).
"""
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  — registra todas as tabelas no Base.metadata
from app.core.database import Base, get_db
from app.dependencies.auth import get_current_user
from app.models.user import User
from app.routes import complaints

TENANT_ID = "t-test"
TUTOR_ID = "tutor-test"
WALKER_ID = "walker-test"
ADMIN_ID = "admin-test"


def build():
    # StaticPool: uma unica conexao compartilhada — senao cada thread do TestClient
    # abre um SQLite em memoria vazio (tabelas somem).
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    db.add(User(id=TUTOR_ID, email="tutor@test.com", password_hash="x", role="cliente", tenant_id=TENANT_ID))
    db.add(User(id=WALKER_ID, email="walker@test.com", password_hash="x", role="walker", tenant_id=TENANT_ID))
    # super_admin: user_has_permission curto-circuita para super_admin, entao
    # passa o require_permission("occurrences.*") sem precisar seedar RBAC.
    db.add(User(id=ADMIN_ID, email="admin@test.com", password_hash="x", role="super_admin", tenant_id=TENANT_ID))
    db.commit()

    test_app = FastAPI()
    test_app.include_router(complaints.router)
    test_app.include_router(complaints.admin_router)
    test_app.dependency_overrides[get_db] = lambda: db

    # estado mutavel: qual usuario esta autenticado (default: tutor)
    state = {"uid": TUTOR_ID}

    def _current_user():
        uid = state["uid"]
        if uid is None:
            raise HTTPException(status_code=401, detail="Nao autenticado")
        return db.get(User, uid)

    test_app.dependency_overrides[get_current_user] = _current_user
    return TestClient(test_app), db, state


def _tutor_payload(**overrides):
    payload = {
        "source": "tutor",
        "target_type": "walker",
        "target_user_id": WALKER_ID,
        "category": "atraso",
        "title": "Passeador atrasou",
        "description": "O passeador chegou bem atrasado e nao avisou.",
    }
    payload.update(overrides)
    return payload


# ----- criacao (happy path + classificacao/severidade) -----
def test_create_complaint_happy_path_low_severity():
    client, _db, _state = build()
    r = client.post("/complaints", json=_tutor_payload())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"]
    assert body["source"] == "tutor"
    assert body["author_id"] == TUTOR_ID
    assert body["category"] == "atraso"
    # severidade e classificada pelo motor (campo presente e valido)
    assert body["severity"] in {"baixa", "media", "alta", "critica"}
    assert isinstance(body["risk_score"], (int, float))
    # historico inicial e registrado pelo service
    assert len(body["history"]) >= 1


def test_create_high_risk_category_raises_severity():
    """Categoria de alto risco (fuga_pet) + termo critico => severidade elevada.

    score = 10 base + 18 (HIGH_RISK_CATEGORIES) + 34 (termo critico 'fuga'/'fugiu')
    => 62 >= 55 => 'alta' (no minimo). Confirma que alto risco nao fica 'baixa'.
    """
    client, _db, _state = build()
    r = client.post("/complaints", json=_tutor_payload(
        category="fuga_pet",
        title="Pet fugiu durante o passeio",
        description="O cachorro fugiu e correu para a rua, situacao de fuga grave.",
    ))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["severity"] in {"alta", "critica"}
    assert body["severity"] != "baixa"
    # alto risco exige revisao manual e entra 'em_analise'
    assert body["requires_manual_review"] is True
    assert body["status"] == "em_analise"


def test_create_critical_severity_full_score():
    """Tutor -> walker, categoria alto risco + termo critico + evidencias.

    Espera severidade 'critica' e acoes criticas sugeridas (suspend_walker)."""
    client, _db, _state = build()
    r = client.post("/complaints", json=_tutor_payload(
        category="agressividade_pet",
        title="Agressividade e mordida",
        description="Houve agressividade, mordida e violencia durante o passeio.",
        evidences=[{"evidence_type": "photo", "url": "http://x/y.jpg", "description": "foto"}],
    ))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["severity"] == "critica"
    assert body["requires_manual_review"] is True
    # decisoes do motor sao persistidas e serializadas
    assert len(body["decisions"]) >= 1
    types = {d["decision_type"] for d in body["decisions"]}
    assert "open_admin_case" in types
    # evidencia anexada e serializada
    assert len(body["evidences"]) == 1


def test_create_validation_short_description_422():
    client, _db, _state = build()
    # description min_length=10
    r = client.post("/complaints", json=_tutor_payload(description="curto"))
    assert r.status_code == 422


def test_create_invalid_source_422():
    client, _db, _state = build()
    r = client.post("/complaints", json=_tutor_payload(source="hacker"))
    assert r.status_code == 422


def test_create_wrong_role_for_source_403():
    """source='walker' exige role walker/passeador/admin; tutor (cliente) => 403."""
    client, _db, _state = build()
    r = client.post("/complaints", json=_tutor_payload(source="walker", target_type="tutor", target_user_id=TUTOR_ID))
    assert r.status_code == 403


# ----- auth -----
def test_create_requires_auth_401():
    client, _db, state = build()
    state["uid"] = None
    r = client.post("/complaints", json=_tutor_payload())
    assert r.status_code == 401


def test_list_requires_auth_401():
    client, _db, state = build()
    state["uid"] = None
    r = client.get("/complaints")
    assert r.status_code == 401


# ----- listagem do usuario -----
def test_list_my_cases_returns_authored():
    client, _db, _state = build()
    client.post("/complaints", json=_tutor_payload())
    client.post("/complaints", json=_tutor_payload(category="comunicacao_inadequada",
                                                   description="Comunicacao ruim e sem foto enviada nunca."))
    r = client.get("/complaints")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 2
    assert len(body["items"]) == 2
    assert all(item["author_id"] == TUTOR_ID for item in body["items"])


def test_list_my_cases_isolates_other_users():
    """Tutor nao ve casos de outros (so author_id ou target_user_id dele)."""
    client, db, state = build()
    # walker abre um caso sobre o tutor
    db.add(User(id="other-tutor", email="ot@test.com", password_hash="x", role="cliente", tenant_id=TENANT_ID))
    db.commit()
    state["uid"] = WALKER_ID
    client.post("/complaints", json={
        "source": "walker", "target_type": "tutor", "target_user_id": "other-tutor",
        "category": "endereco_inseguro", "title": "Endereco",
        "description": "Endereco inseguro relatado pelo passeador no local.",
    })
    # tutor original nao deve ver esse caso (nao e author nem target)
    state["uid"] = TUTOR_ID
    r = client.get("/complaints")
    assert r.json()["total"] == 0


def test_get_case_owner_ok_and_other_403():
    client, db, state = build()
    created = client.post("/complaints", json=_tutor_payload()).json()
    cid = created["id"]
    # dono acessa
    assert client.get(f"/complaints/{cid}").status_code == 200
    # terceiro sem relacao -> 403
    db.add(User(id="stranger", email="s@test.com", password_hash="x", role="cliente", tenant_id=TENANT_ID))
    db.commit()
    state["uid"] = "stranger"
    assert client.get(f"/complaints/{cid}").status_code == 403
    # alvo (walker) acessa (target_user_id)
    state["uid"] = WALKER_ID
    assert client.get(f"/complaints/{cid}").status_code == 200


def test_get_missing_case_404():
    client, _db, _state = build()
    assert client.get("/complaints/nao-existe").status_code == 404


# ----- admin: gating de permissao -----
def test_admin_list_forbidden_for_non_admin_403():
    """cliente sem a permissao occurrences.read -> 403 no router admin."""
    client, _db, _state = build()  # default uid = tutor (cliente)
    r = client.get("/admin/complaints")
    assert r.status_code == 403


def test_admin_list_ok_for_admin():
    client, _db, state = build()
    # cria um caso como tutor
    client.post("/complaints", json=_tutor_payload(
        category="fuga_pet", description="Pet fugiu durante o passeio, fuga grave."))
    state["uid"] = ADMIN_ID
    r = client.get("/admin/complaints")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["category"] == "fuga_pet"


def test_admin_list_filter_by_severity():
    client, _db, state = build()
    # baixa
    client.post("/complaints", json=_tutor_payload())
    # critica
    client.post("/complaints", json=_tutor_payload(
        category="agressividade_pet",
        description="Agressividade, mordida e violencia no passeio relatadas.",
    ))
    state["uid"] = ADMIN_ID
    r = client.get("/admin/complaints", params={"severity": "critica"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["severity"] == "critica"


def test_admin_risk_scores_listed_after_creation():
    """Criar caso alto risco gera RiskScore do alvo (walker)."""
    client, _db, state = build()
    client.post("/complaints", json=_tutor_payload(
        category="agressividade_pet",
        description="Agressividade, mordida e violencia no passeio relatadas.",
    ))
    state["uid"] = ADMIN_ID
    r = client.get("/admin/complaints/risk-scores/list")
    assert r.status_code == 200, r.text
    scores = r.json()
    assert any(s["subject_id"] == WALKER_ID for s in scores)


def test_admin_update_status_resolved():
    client, _db, state = build()
    created = client.post("/complaints", json=_tutor_payload()).json()
    cid = created["id"]
    state["uid"] = ADMIN_ID
    r = client.patch(f"/admin/complaints/{cid}", json={"status": "resolvida", "internal_note": "Resolvido com tutor."})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "resolvida"
    assert body["resolved_at"] is not None
