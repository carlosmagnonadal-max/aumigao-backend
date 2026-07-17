"""Testes de ROTA (camada HTTP) do modulo app/routes/walks.py.

Padrao do projeto (ver tests/test_routes_onda1.py e tests/test_routes_auth.py):
monta um FastAPI MINIMO com apenas o router de walks, SQLite em memoria
(StaticPool), overrides de get_db / get_current_user. NAO importa app.main
(que conecta no banco de PROD).

Cobre:
- POST /walks (criar passeio: matching adiado -> pending_walker_confirmation)
- GET /walks (listar passeios do tutor) + 401 sem auth
- PUT /walks/{id}/status (transicao livre; bloqueio de finalizacao direta -> 400)
- POST /walks/{id}/review (gating: exige ride_completed + revisao operacional aprovada)
- POST /walks/{id}/tip-checkout (mesmo gating de completion aprovado)
"""
import os
from uuid import uuid4

import pytest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 - registra todas as tabelas no Base.metadata
from app.core.database import Base, get_db
from app.dependencies.auth import get_current_user
from app.models.pet import Pet
from app.models.tenant import Tenant
from app.models.user import User
from app.models.walk import Walk
from app.models.walk_completion_review import WalkCompletionReview
from app.models.walk_review import WalkReview
from app.models.walker_profile import WalkerProfile
from app.routes import walks
from app.services.tenant_seed_service import DEFAULT_TENANT_SLUG

TENANT_ID = "t-test"
TUTOR_ID = "tutor-test"
WALKER_ID = "walker-test"
PET_ID = "pet-test"


def build():
    """Monta app minimo com o router de walks e um SQLite em memoria isolado."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    # slug = DEFAULT para default_tenant_id() resolver este tenant sem criar outro.
    db.add(Tenant(id=TENANT_ID, name="Aumigao", slug=DEFAULT_TENANT_SLUG, status="active", plan="business"))
    db.add(User(id=TUTOR_ID, email="tutor@test.com", password_hash="x", role="cliente", tenant_id=TENANT_ID))
    db.add(User(id=WALKER_ID, email="walker@test.com", password_hash="x", role="walker", tenant_id=TENANT_ID, is_active=True))
    db.add(Pet(id=PET_ID, tutor_id=TUTOR_ID, name="Rex", tenant_id=TENANT_ID))
    db.commit()

    test_app = FastAPI()
    test_app.include_router(walks.router)
    test_app.dependency_overrides[get_db] = lambda: db
    test_app.dependency_overrides[get_current_user] = lambda: db.get(User, TUTOR_ID)
    return TestClient(test_app), db


def _walk_create_payload(**extra):
    base = {
        "pet_id": PET_ID,
        "scheduled_date": "2026-07-01T10:00:00",
        "duration_minutes": 45,
        "price": 40.0,
        "pickup_method": "Buscar em casa",
        "address_snapshot": "Rua A, 100 - Centro",
        "notes": "Cuidado com o portao",
    }
    base.update(extra)
    return base


def _seed_completed_walk(db, *, approved_review: bool = True, walker_id: str | None = WALKER_ID,
                         operational_status: str = "ride_completed"):
    """Cria um passeio finalizado operacionalmente, opcionalmente com revisao aprovada."""
    walk = Walk(
        id=str(uuid4()),
        tutor_id=TUTOR_ID,
        tenant_id=TENANT_ID,
        walker_id=walker_id,
        assigned_walker_id=walker_id,
        pet_id=PET_ID,
        scheduled_date="2026-07-01T10:00:00",
        duration_minutes=45,
        price=40.0,
        status="Finalizado",
        operational_status=operational_status,
        walker_selection_mode="auto",
    )
    db.add(walk)
    if approved_review:
        db.add(WalkCompletionReview(
            id=str(uuid4()),
            tenant_id=TENANT_ID,
            walk_id=walk.id,
            walker_user_id=walker_id,
            tutor_user_id=TUTOR_ID,
            status="approved",
        ))
    db.commit()
    return walk


def _seed_walker_profile(db, *, user_id: str = WALKER_ID, photo_url: str | None = "https://cdn.aumigao.app/walkers/foto.jpg"):
    profile = WalkerProfile(
        id=str(uuid4()),
        user_id=user_id,
        full_name="Passeador Teste",
        profile_photo_url=photo_url,
        status="active",
        active_as_walker=True,
    )
    db.add(profile)
    db.commit()
    return profile


def _seed_walk_review(db, *, walk_id: str, rating: int, walker_id: str = WALKER_ID, tutor_id: str = TUTOR_ID):
    review = WalkReview(
        id=str(uuid4()),
        tenant_id=TENANT_ID,
        walk_id=walk_id,
        tutor_id=tutor_id,
        walker_id=walker_id,
        rating=rating,
    )
    db.add(review)
    db.commit()
    return review


# --------------------------------------------------------------- create -----
def test_create_walk_born_ready_dispatches_matching(monkeypatch):
    # Gate OFF (default do conftest): o passeio nasce liberado (pending_walker_confirmation)
    # e AGORA dispara start_matching no proprio create (fix do passeio orfao). Aqui o
    # matching e isolado com um spy para testar so a logica de gate/estado; o teste
    # end-to-end (attempt real criada) esta em test_recurring_plan_credits.py.
    calls = []
    monkeypatch.setattr(walks, "start_matching", lambda walk, db: calls.append(walk.id))
    client, _ = build()
    r = client.post("/walks", json=_walk_create_payload())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tutor_id"] == TUTOR_ID
    assert body["pet_id"] == PET_ID
    assert body["pet_name"] == "Rex"
    # Passeio nasce liberado (status legado Agendado) e o matching foi disparado.
    assert body["operational_status"] == "pending_walker_confirmation"
    assert body["status"] == "Agendado"
    assert body["walker_selection_mode"] == "auto"
    assert calls == [body["id"]]


# --------------------------------------------------- create (gate ON) -------
def test_create_walk_gate_on_born_awaiting_payment(monkeypatch):
    # R7: com o gate ligado e sem cobertura de assinatura, o walk nasce aguardando
    # pagamento e NÃO entra no matching até o webhook liberar.
    monkeypatch.setenv("REQUIRE_PAYMENT_BEFORE_MATCHING", "true")
    monkeypatch.setattr(walks, "consume_credit_if_available", lambda db, tenant, tutor_id: None)
    client, _ = build()
    r = client.post("/walks", json=_walk_create_payload())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["operational_status"] == "awaiting_payment"
    assert body["status"] == "aguardando_pagamento"


def test_create_walk_gate_on_born_awaiting_payment_no_matching(monkeypatch):
    # Projeto A (2 fases): com o gate ligado, o passeio nasce 'awaiting_payment' e
    # NÃO dispara matching no create — nem o coberto por assinatura (que agora só
    # entra no matching via confirm-plan). O passeador não é acionado no create.
    monkeypatch.setenv("REQUIRE_PAYMENT_BEFORE_MATCHING", "true")
    calls = []
    monkeypatch.setattr(walks, "start_matching", lambda walk, db: calls.append(walk.id))
    client, _ = build()
    r = client.post("/walks", json=_walk_create_payload())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["operational_status"] == "awaiting_payment"
    assert body["status"] == "aguardando_pagamento"
    assert calls == []  # nenhum matching no create


def test_confirm_plan_debits_and_triggers_matching(monkeypatch):
    # confirm-plan: consome crédito (mock), promove o walk e dispara o matching.
    monkeypatch.setenv("REQUIRE_PAYMENT_BEFORE_MATCHING", "true")

    class _FakeSub:
        id = "sub-1"

    monkeypatch.setattr(walks, "consume_credit_if_available", lambda db, tenant, tutor_id: _FakeSub())
    calls = []
    monkeypatch.setattr(walks, "start_matching", lambda walk, db: calls.append(walk.id))
    client, _ = build()
    walk_id = client.post("/walks", json=_walk_create_payload()).json()["id"]

    r = client.post(f"/walks/{walk_id}/confirm-plan")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["operational_status"] == "pending_walker_confirmation"
    assert body["status"] == "Agendado"
    assert calls == [walk_id]  # matching disparado no confirm-plan


def test_create_walk_persists_and_appears_in_list():
    client, db = build()
    created = client.post("/walks", json=_walk_create_payload()).json()
    assert db.get(Walk, created["id"]) is not None
    listed = client.get("/walks")
    assert listed.status_code == 200
    ids = [w["id"] for w in listed.json()]
    assert created["id"] in ids


# ----------------------------------------------------------------- list -----
def test_list_walks_requires_auth_401():
    client, _ = build()
    # remove o override de auth -> HTTPBearer(auto_error=False) -> get_current_user real -> 401
    client.app.dependency_overrides.pop(get_current_user, None)
    r = client.get("/walks")
    assert r.status_code == 401


def test_list_walks_lightweight_exposes_walker_photo_url():
    # R14.2: listagem leve (full=false, default) precisa da foto pra aba
    # Agendados do app do tutor (passeios.tsx renderiza item.walkerPhotoUrl).
    client, db = build()
    _seed_walker_profile(db)
    walk = _seed_completed_walk(db, approved_review=False, operational_status="ride_scheduled")
    listed = client.get("/walks").json()
    item = next(w for w in listed if w["id"] == walk.id)
    assert item["walker_photo_url"] == "https://cdn.aumigao.app/walkers/foto.jpg"
    assert item["profile_photo_url"] == "https://cdn.aumigao.app/walkers/foto.jpg"
    # Nao deve vazar sob a chave "photo_url" (reservada pra foto de finalizacao do passeio).
    assert "photo_url" not in item


def test_list_walks_no_walker_assigned_photo_is_none():
    client, db = build()
    walk = Walk(
        id=str(uuid4()), tutor_id=TUTOR_ID, tenant_id=TENANT_ID, pet_id=PET_ID,
        scheduled_date="2026-07-03T10:00:00", duration_minutes=30, price=20.0,
        status="Agendado", operational_status="pending_walker_confirmation",
    )
    db.add(walk)
    db.commit()
    listed = client.get("/walks").json()
    item = next(w for w in listed if w["id"] == walk.id)
    assert item["walker_photo_url"] is None
    assert item["profile_photo_url"] is None


def test_list_walks_only_returns_own_walks_for_tutor():
    client, db = build()
    # passeio do tutor logado
    client.post("/walks", json=_walk_create_payload())
    # passeio de outro tutor (nao deve aparecer)
    db.add(User(id="other-tutor", email="other@test.com", password_hash="x", role="cliente", tenant_id=TENANT_ID))
    db.add(Walk(id="other-walk", tutor_id="other-tutor", tenant_id=TENANT_ID, pet_id=PET_ID,
                scheduled_date="2026-07-02T10:00:00", duration_minutes=30, price=20.0,
                status="Agendado", operational_status="ride_scheduled"))
    db.commit()
    listed = client.get("/walks").json()
    tutor_ids = {w["tutor_id"] for w in listed}
    assert tutor_ids == {TUTOR_ID}


# ------------------------------------------------------- detail (foto/nota) -----
def test_get_walk_exposes_walker_photo_and_rating():
    # R14.2/R14.6: GET /walks/{id} eh o endpoint que a tela de pagamento e a
    # tela do passeio do tutor consomem (getWalkById) — precisa expor a foto
    # e a media/contagem de avaliacao do passeador designado.
    client, db = build()
    _seed_walker_profile(db)
    walk = _seed_completed_walk(db, approved_review=False, operational_status="ride_scheduled")
    # 2 reviews em OUTROS passeios do mesmo passeador -> media 4.5, contagem 2.
    other_walk_1 = _seed_completed_walk(db, approved_review=False, operational_status="ride_completed")
    other_walk_2 = _seed_completed_walk(db, approved_review=False, operational_status="ride_completed")
    _seed_walk_review(db, walk_id=other_walk_1.id, rating=4)
    _seed_walk_review(db, walk_id=other_walk_2.id, rating=5)

    r = client.get(f"/walks/{walk.id}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["walker_photo_url"] == "https://cdn.aumigao.app/walkers/foto.jpg"
    assert body["profile_photo_url"] == "https://cdn.aumigao.app/walkers/foto.jpg"
    assert body["walker_rating_avg"] == 4.5
    assert body["walker_rating_count"] == 2
    # Nao deve vazar sob "photo_url" (colide com a foto de finalizacao do passeio
    # que o app do tutor guarda nesta mesma chave — ver schemas/walk.py).
    assert "photo_url" not in body


def test_get_walk_walker_rating_none_when_no_reviews():
    client, db = build()
    _seed_walker_profile(db)
    walk = _seed_completed_walk(db, approved_review=False, operational_status="ride_scheduled")
    body = client.get(f"/walks/{walk.id}").json()
    assert body["walker_rating_avg"] is None
    assert body["walker_rating_count"] == 0


def test_get_walk_normalizes_local_device_photo_url():
    # Foto local do device (file://) nao eh renderizavel no app — normalize_media_url
    # descarta (mesma regra do /walker/public), senao normalizeWalkerAvatarSource
    # rejeitaria a URI e a foto continuaria sumindo mesmo com o campo exposto.
    client, db = build()
    _seed_walker_profile(db, photo_url="file:///data/user/0/local/photo.jpg")
    walk = _seed_completed_walk(db, approved_review=False, operational_status="ride_scheduled")
    body = client.get(f"/walks/{walk.id}").json()
    assert body["walker_photo_url"] == ""


# --------------------------------------------------------------- status -----
def test_update_status_blocks_direct_completion():
    client, db = build()
    walk = _seed_completed_walk(db, approved_review=False, operational_status="ride_in_progress")
    r = client.put(f"/walks/{walk.id}/status", json={"status": "Finalizado"})
    assert r.status_code == 400
    assert "revis" in r.json()["detail"].lower()


def test_update_status_blocks_ride_completed_operational_value():
    client, db = build()
    walk = _seed_completed_walk(db, approved_review=False, operational_status="ride_in_progress")
    r = client.put(f"/walks/{walk.id}/status", json={"status": "ride_completed"})
    assert r.status_code == 400


def test_update_status_allows_intermediate_transition():
    client, db = build()
    walk = _seed_completed_walk(db, approved_review=False, operational_status="walker_accepted")
    r = client.put(f"/walks/{walk.id}/status", json={"status": "Indo buscar o pet"})
    assert r.status_code == 200, r.text
    assert r.json()["operational_status"] == "walker_arriving"


# --------------------------------------------------------------- review -----
def test_review_happy_path_after_approved_completion():
    client, db = build()
    walk = _seed_completed_walk(db, approved_review=True)
    r = client.post(f"/walks/{walk.id}/review", json={
        "rating": 5, "comment": "Otimo passeio", "tags": ["punctual", "caring"],
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["review"]["rating"] == 5
    assert sorted(body["review"]["tags"]) == ["caring", "punctual"]


def test_review_blocked_without_completed_status():
    client, db = build()
    # revisao aprovada existe, mas o passeio ainda nao esta ride_completed
    walk = _seed_completed_walk(db, approved_review=True, operational_status="ride_in_progress")
    r = client.post(f"/walks/{walk.id}/review", json={"rating": 4})
    assert r.status_code == 409


def test_review_blocked_without_approved_completion_review():
    client, db = build()
    # ride_completed mas SEM WalkCompletionReview aprovada
    walk = _seed_completed_walk(db, approved_review=False)
    r = client.post(f"/walks/{walk.id}/review", json={"rating": 4})
    assert r.status_code == 409


def test_review_rejects_non_owner_tutor_403():
    client, db = build()
    walk = _seed_completed_walk(db, approved_review=True)
    # outro tutor tenta avaliar passeio que nao e dele -> _get_walk_for_user nega com 403
    db.add(User(id="intruder", email="intruder@test.com", password_hash="x", role="cliente", tenant_id=TENANT_ID))
    db.commit()
    client.app.dependency_overrides[get_current_user] = lambda: db.get(User, "intruder")
    r = client.post(f"/walks/{walk.id}/review", json={"rating": 5})
    assert r.status_code == 403


# ----------------------------------------------------------- tip-checkout ----
@pytest.mark.skipif(
    not os.getenv("ASAAS_SANDBOX_API_KEY"),
    reason="requer ASAAS_SANDBOX_API_KEY (chamada real ao sandbox Asaas no caminho feliz); roda local/com credencial, pula no CI",
)
def test_tip_checkout_happy_path_after_approved_completion():
    client, db = build()
    walk = _seed_completed_walk(db, approved_review=True)
    r = client.post(f"/walks/{walk.id}/tip-checkout", json={"amount": 10.0})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "pending"
    assert body["tip_id"]
    # checkout_url pode ser deep-link interno (mock) ou URL real do Asaas (sandbox/live)
    checkout_url = body.get("checkout_url") or ""
    assert checkout_url  # não pode ser vazio
    assert checkout_url.startswith("aumigao://tip-checkout/") or checkout_url.startswith("http")


def test_tip_checkout_blocked_without_completed_status():
    client, db = build()
    walk = _seed_completed_walk(db, approved_review=True, operational_status="ride_in_progress")
    r = client.post(f"/walks/{walk.id}/tip-checkout", json={"amount": 10.0})
    assert r.status_code == 409


def test_tip_checkout_blocked_without_approved_completion_review():
    client, db = build()
    walk = _seed_completed_walk(db, approved_review=False)
    r = client.post(f"/walks/{walk.id}/tip-checkout", json={"amount": 10.0})
    assert r.status_code == 409


def test_tip_checkout_requires_auth_401():
    client, db = build()
    walk = _seed_completed_walk(db, approved_review=True)
    client.app.dependency_overrides.pop(get_current_user, None)
    r = client.post(f"/walks/{walk.id}/tip-checkout", json={"amount": 10.0})
    assert r.status_code == 401


def test_create_walk_snapshots_tutor_profile_address_when_empty():
    """BUG 09/07: o app envia address_snapshot="" fixo — a criação deve tirar o
    snapshot do endereço do PERFIL do tutor (cadastro step-2) na hora."""
    from app.models.tutor_profile import TutorProfile
    client, db = build()
    db.add(TutorProfile(
        id="tp-test", user_id=TUTOR_ID, cep="41760150", street="Av. Paralela",
        number="3500", neighborhood="Trobogy", city="Salvador", state="BA",
    ))
    db.commit()
    r = client.post("/walks", json=_walk_create_payload(address_snapshot=""))
    assert r.status_code == 200, r.text
    walk = db.get(Walk, r.json()["id"])
    assert "Av. Paralela" in (walk.address_snapshot or "")
    assert "Trobogy" in (walk.address_snapshot or "")


def test_create_walk_keeps_explicit_address_snapshot():
    client, db = build()
    r = client.post("/walks", json=_walk_create_payload())
    assert r.status_code == 200, r.text
    walk = db.get(Walk, r.json()["id"])
    assert walk.address_snapshot == "Rua A, 100 - Centro"
