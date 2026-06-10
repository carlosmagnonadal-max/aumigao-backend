"""Testes de ROTA (camada HTTP) do grupo complaints-risk do admin.

O grupo "complaints-risk" vive em app/routes/complaints.py (NAO em admin.py):
listar/detalhar/classificar ocorrencias, atualizar status/severidade, aplicar
decisoes (acoes), listar RiskScore e as rotas legadas /admin/occurrences.

Padrao do projeto (ver tests/test_routes_walker_quality.py e test_routes_auth.py):
FastAPI MINIMO so com os routers de complaints + overrides de get_db /
get_current_user (SQLite em memoria, StaticPool). NAO importa app.main (Neon PROD).

Gating de permissao:
- admin_router exige require_permission("occurrences.read") no nivel do router;
  alguns endpoints (PATCH update, POST decision, /action legado) exigem
  "occurrences.manage". super_admin passa em ambos (rede de seguranca em rbac);
  um usuario role "tutor" recebe 403.
"""
from datetime import datetime
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 - registra todas as tabelas no Base.metadata
from app.core.database import Base, get_db
from app.dependencies.auth import get_current_user
from app.models.complaint import Complaint, ComplaintDecision, RiskScore
from app.models.user import User
from app.routes import complaints

ADMIN_ID = "admin-1"
TUTOR_ID = "tutor-1"


def build(*, current: str = ADMIN_ID):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    # super_admin -> bypassa RBAC (occurrences.read e occurrences.manage)
    db.add(User(id=ADMIN_ID, email="admin@test.com", password_hash="x", role="super_admin"))
    # tutor comum -> sem occurrences.* -> 403
    db.add(User(id=TUTOR_ID, email="tutor@test.com", password_hash="x", role="tutor"))
    db.commit()

    test_app = FastAPI()
    test_app.include_router(complaints.admin_router)
    test_app.include_router(complaints.legacy_admin_occurrences_router)
    test_app.dependency_overrides[get_db] = lambda: db
    test_app.dependency_overrides[get_current_user] = lambda: db.get(User, current)
    return TestClient(test_app), db


def set_user(client, db, user_id):
    client.app.dependency_overrides[get_current_user] = lambda: db.get(User, user_id)


def seed_complaint(db, *, cid=None, status="em_analise", severity="media",
                   category="atraso", target_user_id="walker-x", walk_id=None,
                   author_id="tutor-author"):
    cid = cid or str(uuid4())
    c = Complaint(
        id=cid,
        source="tutor",
        author_id=author_id,
        author_role="tutor",
        target_type="walker",
        target_user_id=target_user_id,
        walk_id=walk_id,
        category=category,
        severity=severity,
        status=status,
        title="Ocorrencia",
        description="Descricao da ocorrencia de teste",
        risk_score=42.0,
        requires_manual_review=True,
        recurrence_count=0,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(c)
    db.commit()
    return c


# --------------------------------------------------- listar ocorrencias ------
def test_admin_list_requires_permission():
    client, db = build()
    set_user(client, db, TUTOR_ID)  # sem occurrences.read
    r = client.get("/admin/complaints")
    assert r.status_code == 403


def test_admin_list_happy_path_and_total():
    client, db = build()
    seed_complaint(db, cid="c-1")
    seed_complaint(db, cid="c-2")
    r = client.get("/admin/complaints")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 2
    ids = {item["id"] for item in body["items"]}
    assert ids == {"c-1", "c-2"}


def test_admin_list_filter_by_status():
    client, db = build()
    seed_complaint(db, cid="aberta-1", status="aberta")
    seed_complaint(db, cid="resolvida-1", status="resolvida")
    r = client.get("/admin/complaints", params={"status": "resolvida"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["id"] == "resolvida-1"
    # status="all" nao filtra
    assert client.get("/admin/complaints", params={"status": "all"}).json()["total"] == 2


def test_admin_list_filter_by_severity_and_walker():
    client, db = build()
    seed_complaint(db, cid="crit", severity="critica", target_user_id="walker-A")
    seed_complaint(db, cid="baixa", severity="baixa", target_user_id="walker-B")
    # filtro severity
    r = client.get("/admin/complaints", params={"severity": "critica"})
    assert {i["id"] for i in r.json()["items"]} == {"crit"}
    # filtro walker_id (mapeia para target_user_id)
    r2 = client.get("/admin/complaints", params={"walker_id": "walker-B"})
    assert {i["id"] for i in r2.json()["items"]} == {"baixa"}


# --------------------------------------------------- detalhar ocorrencia -----
def test_admin_get_case_happy_path():
    client, db = build()
    seed_complaint(db, cid="c-detail")
    r = client.get("/admin/complaints/c-detail")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == "c-detail"
    assert isinstance(body["evidences"], list)
    assert isinstance(body["decisions"], list)
    assert isinstance(body["history"], list)


def test_admin_get_case_404_for_unknown():
    client, _ = build()
    r = client.get("/admin/complaints/does-not-exist")
    assert r.status_code == 404


def test_admin_get_case_requires_permission():
    client, db = build()
    seed_complaint(db, cid="c-x")
    set_user(client, db, TUTOR_ID)
    r = client.get("/admin/complaints/c-x")
    assert r.status_code == 403


# --------------------------------------------------- atualizar (manage) ------
def test_admin_update_changes_status_and_severity():
    client, db = build()
    seed_complaint(db, cid="c-upd", status="em_analise", severity="media")
    r = client.patch("/admin/complaints/c-upd", json={
        "status": "resolvida", "severity": "alta", "internal_note": "encerrada apos analise",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "resolvida"
    assert body["severity"] == "alta"
    # transicao para resolvida -> resolved_at preenchido
    db.expire_all()
    c = db.get(Complaint, "c-upd")
    assert c.resolved_at is not None
    assert c.resolved_by_admin_id == ADMIN_ID
    # historico registrado
    assert len(c.history) >= 1


def test_admin_update_requires_manage_permission():
    client, db = build()
    seed_complaint(db, cid="c-upd2")
    set_user(client, db, TUTOR_ID)  # nao tem occurrences.manage (nem read)
    r = client.patch("/admin/complaints/c-upd2", json={"status": "resolvida"})
    assert r.status_code == 403


def test_admin_update_404_for_unknown():
    client, _ = build()
    r = client.patch("/admin/complaints/nope", json={"status": "resolvida"})
    assert r.status_code == 404


def test_admin_update_rejects_invalid_status_enum():
    client, db = build()
    seed_complaint(db, cid="c-enum")
    r = client.patch("/admin/complaints/c-enum", json={"status": "status_invalido"})
    assert r.status_code == 422


# --------------------------------------------------- decisao / acoes ---------
def test_admin_decision_creates_and_reviews():
    client, db = build()
    seed_complaint(db, cid="c-dec")
    r = client.post("/admin/complaints/c-dec/decision", json={
        "decision_type": "temporarily_suspend_walker",
        "decision_status": "applied",
        "reason": "Reincidencia comprovada na analise.",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    # decisao persistida no caso
    decisions = body["decisions"]
    match = [d for d in decisions if d["decision_type"] == "temporarily_suspend_walker"]
    assert match and match[0]["decision_status"] == "applied"
    assert match[0]["reviewed_by_admin_id"] == ADMIN_ID


def test_admin_decision_updates_existing_decision():
    client, db = build()
    seed_complaint(db, cid="c-dec2")
    db.add(ComplaintDecision(
        id="dec-existing", complaint_id="c-dec2", decision_type="review_refund",
        decision_status="suggested", severity_snapshot="media", reason="auto",
        created_by="decision_engine", created_at=datetime.utcnow(),
    ))
    db.commit()
    r = client.post("/admin/complaints/c-dec2/decision", json={
        "decision_type": "review_refund",
        "decision_status": "rejected",
        "reason": "Reembolso indevido apos revisao.",
    })
    assert r.status_code == 200, r.text
    # reaproveita a decisao existente (nao cria duplicata)
    refund = [d for d in r.json()["decisions"] if d["decision_type"] == "review_refund"]
    assert len(refund) == 1
    assert refund[0]["decision_status"] == "rejected"


def test_admin_decision_requires_manage_permission():
    client, db = build()
    seed_complaint(db, cid="c-dec3")
    set_user(client, db, TUTOR_ID)
    r = client.post("/admin/complaints/c-dec3/decision", json={
        "decision_type": "review_refund", "decision_status": "approved",
        "reason": "qualquer motivo aqui",
    })
    assert r.status_code == 403


def test_admin_decision_rejects_short_reason():
    client, db = build()
    seed_complaint(db, cid="c-dec4")
    r = client.post("/admin/complaints/c-dec4/decision", json={
        "decision_type": "review_refund", "decision_status": "approved", "reason": "curto",
    })
    assert r.status_code == 422


# --------------------------------------------------- risk scores -------------
def test_admin_risk_scores_list_sorted_desc():
    client, db = build()
    db.add(RiskScore(id="r-low", subject_type="walker", subject_id="w1", score=10.0,
                     severity="normal", complaints_count=1, updated_at=datetime.utcnow()))
    db.add(RiskScore(id="r-high", subject_type="walker", subject_id="w2", score=80.0,
                     severity="critico", complaints_count=3, critical_count=2,
                     updated_at=datetime.utcnow()))
    db.add(RiskScore(id="r-pet", subject_type="pet", subject_id="p1", score=50.0,
                     severity="alto", updated_at=datetime.utcnow()))
    db.commit()
    r = client.get("/admin/complaints/risk-scores/list")
    assert r.status_code == 200, r.text
    items = r.json()
    assert [i["id"] for i in items] == ["r-high", "r-pet", "r-low"]  # ordem por score desc


def test_admin_risk_scores_filter_by_subject_type():
    client, db = build()
    db.add(RiskScore(id="r-w", subject_type="walker", subject_id="w1", score=10.0,
                     updated_at=datetime.utcnow()))
    db.add(RiskScore(id="r-p", subject_type="pet", subject_id="p1", score=20.0,
                     updated_at=datetime.utcnow()))
    db.commit()
    r = client.get("/admin/complaints/risk-scores/list", params={"subject_type": "pet"})
    assert r.status_code == 200, r.text
    assert [i["id"] for i in r.json()] == ["r-p"]


def test_admin_risk_scores_requires_permission():
    client, db = build()
    set_user(client, db, TUTOR_ID)
    r = client.get("/admin/complaints/risk-scores/list")
    assert r.status_code == 403


# --------------------------------------------------- legado /admin/occurrences
def test_legacy_occurrences_list_payload_shape():
    client, db = build()
    seed_complaint(db, cid="leg-1", category="contratacao_por_fora", severity="alta")
    r = client.get("/admin/occurrences")
    assert r.status_code == 200, r.text
    items = r.json()
    assert isinstance(items, list)
    item = next(i for i in items if i["id"] == "leg-1")
    # payload customizado (complaint_admin_payload), nao o ComplaintResponse
    assert item["occurrence_status"] == "alta" or "occurrence_status" in item
    assert item["category"] == "contratacao_por_fora"
    assert item["severity"] == "alta"
    assert "risk_score" in item


def test_legacy_occurrences_action_mark_resolved():
    client, db = build()
    seed_complaint(db, cid="leg-res", status="em_analise")
    r = client.post("/admin/occurrences/leg-res/action", json={
        "action": "mark_resolved", "note": "Resolvido manualmente",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["occurrence_status"] == "resolvida"
    db.expire_all()
    assert db.get(Complaint, "leg-res").status == "resolvida"


def test_legacy_occurrences_action_requires_manage_permission():
    client, db = build()
    seed_complaint(db, cid="leg-403")
    set_user(client, db, TUTOR_ID)
    r = client.post("/admin/occurrences/leg-403/action", json={"action": "mark_resolved"})
    assert r.status_code == 403


def test_legacy_occurrences_action_default_creates_decision():
    client, db = build()
    seed_complaint(db, cid="leg-dec")
    # acao desconhecida -> cai em admin_review_decision (cria decisao)
    r = client.post("/admin/occurrences/leg-dec/action", json={"action": "reduce_walker_ranking"})
    assert r.status_code == 200, r.text
    db.expire_all()
    c = db.get(Complaint, "leg-dec")
    types = {d.decision_type for d in c.decisions}
    assert "reduce_walker_ranking" in types
