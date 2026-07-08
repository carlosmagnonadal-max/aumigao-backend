"""ITEM 7 — Checklist de finalização como REGISTRO/CONFIRMAÇÃO, não obrigação de método.

Garante que:
1. Finalização de passeio funciona SEM preencher nenhum campo do checklist
   (checklist ausente, vazio ou parcial NÃO bloqueia o endpoint).
2. Finalização com checklist preenchido também funciona normalmente.
3. O fluxo de pagamento/comissão permanece intacto — a aprovação pelo admin
   continua criando o Payment (coberto por test_routes_admin_completion_review.py).
4. incident_reported continua disponível (pode ser enviado) mas não é obrigatório.

Padrão do projeto: FastAPI mínimo + SQLite em memória + override de get_db/get_current_user.
"""
from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 — registra todas as tabelas no Base.metadata
from app.core.database import Base, get_db, get_walker_self_db
from app.dependencies.auth import get_current_user
from app.models.tenant import Tenant
from app.models.user import User
from app.models.walk import Walk
from app.models.walk_completion_review import WalkCompletionReview
from app.models.walker_profile import WalkerProfile
from app.routes import walker
from app.services.tenant_seed_service import DEFAULT_TENANT_SLUG

# IDs estáticos para os testes
TENANT_ID = "t-checklist"
WALKER_ID = "walker-checklist"
TUTOR_ID = "tutor-checklist"


def _build():
    """Monta app mínimo com router walker, SQLite em memória."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    db.add(Tenant(id=TENANT_ID, name="Aumigao", slug=DEFAULT_TENANT_SLUG, status="active", plan="business"))
    db.add(User(id=WALKER_ID, email="walker@chk.com", password_hash="x", role="walker",
                tenant_id=TENANT_ID, full_name="Passeador Checklist"))
    db.add(User(id=TUTOR_ID, email="tutor@chk.com", password_hash="x", role="tutor",
                tenant_id=TENANT_ID, full_name="Tutor Checklist"))
    # WalkerProfile é exigido por _require_active_walker
    db.add(WalkerProfile(
        id="wp-chk",
        user_id=WALKER_ID,
        full_name="Passeador Checklist",
        status="active",
        active_as_walker=True,
    ))
    db.commit()

    test_app = FastAPI()
    test_app.include_router(walker.router)
    test_app.dependency_overrides[get_db] = lambda: db
    test_app.dependency_overrides[get_walker_self_db] = lambda: db
    test_app.dependency_overrides[get_current_user] = lambda: db.get(User, WALKER_ID)
    return TestClient(test_app), db


def _make_walk(db, op_status="ride_in_progress") -> Walk:
    """Cria um passeio em estado que permite envio de completion-report."""
    w = Walk(
        id="walk-chk-1",
        tutor_id=TUTOR_ID,
        walker_id=WALKER_ID,
        pet_id="pet-1",
        scheduled_date="2024-06-01T10:00:00",
        duration_minutes=30,
        price=50.0,
        status="Passeando agora",
        operational_status=op_status,
        created_at=datetime.utcnow(),
    )
    db.add(w)
    db.commit()
    return w


# --------------------------------------------------------------------------- #
# Testes: finalização SEM checklist (checklist ausente/vazio/parcial)
# --------------------------------------------------------------------------- #

class TestCompletionWithoutChecklist:
    """A finalização não pode ser bloqueada por campos ausentes no checklist."""

    def test_completion_without_checklist_field_succeeds(self):
        """Enviar relatório sem campo checklist deve retornar 200."""
        client, db = _build()
        _make_walk(db)

        r = client.post(
            "/walker/walks/walk-chk-1/completion-report",
            json={"photo_url": "https://example.com/foto.jpg", "notes": "Passeio finalizado"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True
        assert body["review"]["status"] == "pending_review"

    def test_completion_with_empty_checklist_succeeds(self):
        """Checklist vazio ({}) não bloqueia a finalização."""
        client, db = _build()
        _make_walk(db)

        r = client.post(
            "/walker/walks/walk-chk-1/completion-report",
            json={
                "photo_url": "https://example.com/foto.jpg",
                "notes": "Tudo tranquilo",
                "checklist": {},
            },
        )
        assert r.status_code == 200, r.text
        assert r.json()["ok"] is True

    def test_completion_with_null_checklist_succeeds(self):
        """Checklist null/None não bloqueia a finalização."""
        client, db = _build()
        _make_walk(db)

        r = client.post(
            "/walker/walks/walk-chk-1/completion-report",
            json={
                "photo_url": "https://example.com/foto.jpg",
                "notes": "Passeio ok",
                "checklist": None,
            },
        )
        assert r.status_code == 200, r.text

    def test_completion_with_partial_checklist_succeeds(self):
        """Checklist com apenas alguns campos (não todos) não bloqueia."""
        client, db = _build()
        _make_walk(db)

        r = client.post(
            "/walker/walks/walk-chk-1/completion-report",
            json={
                "photo_url": "https://example.com/foto.jpg",
                "notes": "Quase tudo ok",
                "checklist": {"pet_delivered": True},  # apenas 1 dos 4 campos
            },
        )
        assert r.status_code == 200, r.text

    def test_completion_with_only_incident_reported_succeeds(self):
        """incident_reported pode ser enviado isoladamente (valor de segurança)."""
        client, db = _build()
        _make_walk(db)

        r = client.post(
            "/walker/walks/walk-chk-1/completion-report",
            json={
                "photo_url": "https://example.com/foto.jpg",
                "notes": "Houve um incidente",
                "checklist": {"incident_reported": True},
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        # O campo incident_reported deve estar gravado no checklist
        checklist = body["review"]["checklist"]
        assert checklist.get("incident_reported") is True

    def test_walk_transitions_to_awaiting_review_without_checklist(self):
        """Passeio avança para awaiting_completion_review mesmo sem checklist."""
        client, db = _build()
        _make_walk(db)

        r = client.post(
            "/walker/walks/walk-chk-1/completion-report",
            json={"photo_url": "https://example.com/foto.jpg", "notes": "Finalizado ok"},
        )
        assert r.status_code == 200, r.text
        walk_status = r.json()["walk"]["operational_status"]
        assert walk_status == "awaiting_completion_review"

    def test_completion_status_queued_for_admin_review(self):
        """Relatório enviado fica 'pending_review' aguardando admin — não auto-aprovado."""
        client, db = _build()
        _make_walk(db)

        r = client.post(
            "/walker/walks/walk-chk-1/completion-report",
            json={"photo_url": "https://example.com/foto.jpg", "notes": "Sem checklist"},
        )
        assert r.status_code == 200, r.text
        review = r.json()["review"]
        # Fica em pending_review (admin precisa aprovar para o pagamento)
        assert review["status"] == "pending_review"

    def test_resubmission_endpoint_also_allows_no_checklist(self):
        """Endpoint /report (alias) também aceita sem checklist."""
        client, db = _build()
        _make_walk(db)

        r = client.post(
            "/walker/walks/walk-chk-1/report",
            json={"photo_url": "https://example.com/foto.jpg", "notes": "Via report"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["ok"] is True


class TestCompletionWithChecklist:
    """Quando checklist é enviado, deve ser registrado normalmente."""

    def test_completion_with_full_checklist_succeeds(self):
        """Checklist completo também funciona normalmente."""
        client, db = _build()
        _make_walk(db)

        r = client.post(
            "/walker/walks/walk-chk-1/completion-report",
            json={
                "photo_url": "https://example.com/foto.jpg",
                "notes": "Tudo perfeito",
                "checklist": {
                    "pet_delivered": True,
                    "leash_returned": True,
                    "water_offered": True,
                    "incident_reported": False,
                },
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True
        checklist = body["review"]["checklist"]
        assert checklist["pet_delivered"] is True
        assert checklist["leash_returned"] is True
        assert checklist["water_offered"] is True
        assert checklist["incident_reported"] is False

    def test_checklist_missing_fields_default_to_false(self):
        """Campos não enviados no checklist devem ficar False (não ocorreram/não informados)."""
        client, db = _build()
        _make_walk(db)

        r = client.post(
            "/walker/walks/walk-chk-1/completion-report",
            json={
                "photo_url": "https://example.com/foto.jpg",
                "notes": "Parcialmente preenchido",
                "checklist": {"pet_delivered": True},
            },
        )
        assert r.status_code == 200, r.text
        checklist = r.json()["review"]["checklist"]
        assert checklist["pet_delivered"] is True
        # Campos ausentes -> False
        assert checklist["leash_returned"] is False
        assert checklist["water_offered"] is False
        assert checklist["incident_reported"] is False


class TestCompletionRequiredFieldsStillEnforced:
    """photo_url e notes ainda são obrigatórios (não são controle de método)."""

    def test_missing_photo_url_returns_422(self):
        """photo_url continua obrigatório — é evidência do passeio, não método."""
        client, db = _build()
        _make_walk(db)

        r = client.post(
            "/walker/walks/walk-chk-1/completion-report",
            json={"notes": "Sem foto"},
        )
        assert r.status_code == 422, r.text

    def test_short_notes_returns_422(self):
        """notes com menos de 8 caracteres ainda retorna 422."""
        client, db = _build()
        _make_walk(db)

        r = client.post(
            "/walker/walks/walk-chk-1/completion-report",
            json={"photo_url": "https://example.com/foto.jpg", "notes": "curto"},
        )
        assert r.status_code == 422, r.text

    def test_wrong_operational_status_returns_409(self):
        """Passeio não em ride_in_progress ainda retorna 409."""
        client, db = _build()
        _make_walk(db, op_status="ride_completed")

        r = client.post(
            "/walker/walks/walk-chk-1/completion-report",
            json={"photo_url": "https://example.com/foto.jpg", "notes": "Status errado"},
        )
        assert r.status_code == 409, r.text


# --------------------------------------------------------------------------- #
# Testes diretos do helper _normalize_completion_checklist
# --------------------------------------------------------------------------- #
from app.routes.walker import _normalize_completion_checklist, COMPLETION_CHECKLIST_KNOWN_KEYS


class TestNormalizeCompletionChecklist:
    """Testa o helper diretamente para garantir sem bloqueio."""

    def test_empty_payload_returns_all_false(self):
        result = _normalize_completion_checklist({})
        assert result == {key: False for key in COMPLETION_CHECKLIST_KNOWN_KEYS}

    def test_no_checklist_key_returns_all_false(self):
        result = _normalize_completion_checklist({"photo_url": "x", "notes": "y"})
        assert result == {key: False for key in COMPLETION_CHECKLIST_KNOWN_KEYS}

    def test_partial_checklist_fills_missing_with_false(self):
        result = _normalize_completion_checklist({"checklist": {"pet_delivered": True}})
        assert result["pet_delivered"] is True
        assert result["leash_returned"] is False
        assert result["water_offered"] is False
        assert result["incident_reported"] is False

    def test_full_checklist_preserved(self):
        payload = {
            "checklist": {
                "pet_delivered": True,
                "leash_returned": True,
                "water_offered": False,
                "incident_reported": True,
            }
        }
        result = _normalize_completion_checklist(payload)
        assert result["pet_delivered"] is True
        assert result["leash_returned"] is True
        assert result["water_offered"] is False
        assert result["incident_reported"] is True

    def test_string_json_checklist_parsed(self):
        import json
        payload = {"checklist": json.dumps({"pet_delivered": True})}
        result = _normalize_completion_checklist(payload)
        assert result["pet_delivered"] is True
        assert result["leash_returned"] is False

    def test_invalid_checklist_type_becomes_empty(self):
        result = _normalize_completion_checklist({"checklist": "not-valid-json!!!"})
        assert result == {key: False for key in COMPLETION_CHECKLIST_KNOWN_KEYS}

    def test_null_checklist_returns_all_false(self):
        result = _normalize_completion_checklist({"checklist": None})
        assert result == {key: False for key in COMPLETION_CHECKLIST_KNOWN_KEYS}

    def test_does_not_raise_on_missing_keys(self):
        """Garantia explícita: ausência de campos NÃO levanta HTTPException."""
        from fastapi import HTTPException
        try:
            _normalize_completion_checklist({})
        except HTTPException:
            pytest.fail("_normalize_completion_checklist levantou HTTPException com checklist vazio — não deve bloquear.")


# --------------------------------------------------------------------------- #
# Regressão 2026-07-08: review DEVE herdar o tenant_id do walk (RLS de prod
# rejeitava o INSERT com tenant_id NULL — 500 no Enviar para revisão).
# --------------------------------------------------------------------------- #

class TestCompletionReviewTenantScope:
    def test_review_inherits_walk_tenant_id(self):
        client, db = _build()
        w = Walk(
            id="walk-chk-tenant",
            tenant_id=TENANT_ID,
            tutor_id=TUTOR_ID,
            walker_id=WALKER_ID,
            pet_id="pet-1",
            scheduled_date="2024-06-01T10:00:00",
            duration_minutes=30,
            price=50.0,
            status="Passeando agora",
            operational_status="ride_in_progress",
            created_at=datetime.utcnow(),
        )
        db.add(w)
        db.commit()

        r = client.post(
            "/walker/walks/walk-chk-tenant/completion-report",
            json={"photo_url": "https://example.com/foto.jpg", "notes": "Passeio finalizado"},
        )
        assert r.status_code == 200, r.text
        review = (
            db.query(WalkCompletionReview)
            .filter(WalkCompletionReview.walk_id == "walk-chk-tenant")
            .one()
        )
        assert review.tenant_id == TENANT_ID


# --------------------------------------------------------------------------- #
# Registro estruturado (2026-07-08): campos de texto opcionais preservados no
# checklist_json — entrega a quem + descrição de ocorrência.
# --------------------------------------------------------------------------- #

class TestCompletionStructuredFields:
    def test_delivered_to_other_and_incident_description_preserved(self):
        client, db = _build()
        _make_walk(db)

        r = client.post(
            "/walker/walks/walk-chk-1/completion-report",
            json={
                "photo_url": "https://example.com/foto.jpg",
                "notes": "Passeio finalizado",
                "checklist": {
                    "pet_delivered": True,
                    "water_offered": True,
                    "incident_reported": True,
                    "delivered_to": "other",
                    "delivered_to_name": "  Porteiro João  ",
                    "incident_description": "Latiu para outro cão, sem ferimentos.",
                },
            },
        )
        assert r.status_code == 200, r.text
        checklist = r.json()["review"]["checklist"]
        assert checklist["pet_delivered"] is True
        assert checklist["delivered_to"] == "other"
        assert checklist["delivered_to_name"] == "Porteiro João"
        assert checklist["incident_description"] == "Latiu para outro cão, sem ferimentos."

    def test_delivered_to_tutor_omits_name(self):
        client, db = _build()
        _make_walk(db)

        r = client.post(
            "/walker/walks/walk-chk-1/completion-report",
            json={
                "photo_url": "https://example.com/foto.jpg",
                "notes": "Tudo tranquilo",
                "checklist": {"pet_delivered": True, "delivered_to": "tutor", "delivered_to_name": "ignorado"},
            },
        )
        assert r.status_code == 200, r.text
        checklist = r.json()["review"]["checklist"]
        assert checklist["delivered_to"] == "tutor"
        assert "delivered_to_name" not in checklist

    def test_invalid_delivered_to_dropped(self):
        from app.routes.walker import _normalize_completion_checklist
        result = _normalize_completion_checklist({"checklist": {"delivered_to": "hacker", "delivered_to_name": "x"}})
        assert "delivered_to" not in result
        assert "delivered_to_name" not in result

    def test_bools_unchanged_without_structured_fields(self):
        from app.routes.walker import _normalize_completion_checklist
        result = _normalize_completion_checklist({"checklist": {"pet_delivered": True}})
        assert result == {
            "pet_delivered": True,
            "leash_returned": False,
            "water_offered": False,
            "incident_reported": False,
        }
