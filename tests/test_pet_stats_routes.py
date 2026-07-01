"""T17 — Testes do endpoint GET /api/pets/{pet_id}/stats (Fase 5 — gráficos)."""
from __future__ import annotations

import app.models  # noqa: F401

import json
from datetime import datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.dependencies.auth import get_current_user
from app.models.pet import Pet
from app.models.pet_profile_config import PetProfileConfig
from app.models.pet_timeline_event import PetTimelineEvent
from app.models.tenant import Tenant, TenantFeature
from app.models.user import User
from app.models.walk import Walk
from app.models.walk_observation import WalkObservation
from app.routes import pet_profile as routes


def _ctx(profile_active=True):
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()

    db.add(Tenant(id="t1", name="T1", slug="t1", status="active", plan="business"))
    db.add(User(id="u1", email="u1@x.com", password_hash="x", role="tutor", tenant_id="t1"))
    db.add(User(id="u2", email="u2@x.com", password_hash="x", role="tutor", tenant_id="t1"))
    db.add(User(id="walker1", email="w@x.com", password_hash="x", role="walker", tenant_id="t1"))
    db.add(Pet(id="p1", tutor_id="u1", tenant_id="t1", name="Rex"))

    if profile_active:
        db.add(TenantFeature(tenant_id="t1", feature_key="pet_live_profile", enabled=True))
        db.add(PetProfileConfig(tenant_id="t1", profile_enabled=True))

    db.commit()
    return db


def _client(db, user, env_on=True, monkeypatch=None):
    if monkeypatch:
        if env_on:
            monkeypatch.setenv("PET_LIVE_PROFILE_ENABLED", "true")
        else:
            monkeypatch.delenv("PET_LIVE_PROFILE_ENABLED", raising=False)

    app = FastAPI()
    app.include_router(routes.api_router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app)


# ---------------------------------------------------------------------------
# Shape completo com dados
# ---------------------------------------------------------------------------

def test_stats_returns_complete_shape(monkeypatch):
    """Endpoint retorna todas as chaves esperadas mesmo sem dados."""
    db = _ctx()
    c = _client(db, db.get(User, "u1"), monkeypatch=monkeypatch)

    r = c.get("/api/pets/p1/stats")
    assert r.status_code == 200, r.text
    body = r.json()

    assert "weight_series" in body
    assert "walks_per_week" in body
    assert "observations" in body

    obs = body["observations"]
    assert "total" in obs
    assert "mood" in obs
    assert "energy" in obs
    assert "socialization" in obs
    assert "peed_pct" in obs
    assert "pooped_pct" in obs
    assert "incidents" in obs

    # Mood: TODAS as chaves presentes mesmo zeradas
    for key in ("calm", "happy", "anxious", "agitated"):
        assert key in obs["mood"], f"mood key missing: {key}"
    for key in ("low", "normal", "high"):
        assert key in obs["energy"], f"energy key missing: {key}"
    for key in ("good", "neutral", "reactive"):
        assert key in obs["socialization"], f"socialization key missing: {key}"


def test_stats_walks_per_week_has_12_entries(monkeypatch):
    """walks_per_week sempre tem 12 entradas (mesmo sem dados)."""
    db = _ctx()
    c = _client(db, db.get(User, "u1"), monkeypatch=monkeypatch)

    r = c.get("/api/pets/p1/stats")
    assert r.status_code == 200
    assert len(r.json()["walks_per_week"]) == 12


def test_stats_empty_pet_returns_zeros_and_nulls(monkeypatch):
    """Pet sem dados → séries vazias, peed_pct/pooped_pct null."""
    db = _ctx()
    c = _client(db, db.get(User, "u1"), monkeypatch=monkeypatch)

    r = c.get("/api/pets/p1/stats")
    assert r.status_code == 200
    body = r.json()
    assert body["weight_series"] == []
    assert all(e["count"] == 0 for e in body["walks_per_week"])
    obs = body["observations"]
    assert obs["total"] == 0
    assert obs["peed_pct"] is None
    assert obs["pooped_pct"] is None
    assert obs["incidents"] == 0


def test_stats_weight_series_from_events(monkeypatch):
    """Eventos weight com kg no payload_json → aparecem em weight_series."""
    db = _ctx()
    db.add(PetTimelineEvent(
        pet_id="p1", tenant_id="t1", event_type="weight",
        title="Peso", occurred_at=datetime(2026, 5, 1),
        payload_json=json.dumps({"kg": 10.5}), source="tutor",
    ))
    db.add(PetTimelineEvent(
        pet_id="p1", tenant_id="t1", event_type="weight",
        title="Peso 2", occurred_at=datetime(2026, 6, 1),
        payload_json=json.dumps({"kg": 11.0}), source="tutor",
    ))
    db.commit()

    c = _client(db, db.get(User, "u1"), monkeypatch=monkeypatch)
    r = c.get("/api/pets/p1/stats")
    assert r.status_code == 200
    series = r.json()["weight_series"]
    assert len(series) == 2
    assert series[0]["kg"] == 10.5
    assert series[1]["kg"] == 11.0


def test_stats_malformed_weight_ignored(monkeypatch):
    """Evento weight com payload malformado é ignorado silenciosamente."""
    db = _ctx()
    db.add(PetTimelineEvent(
        pet_id="p1", tenant_id="t1", event_type="weight",
        title="Peso malformado", occurred_at=datetime(2026, 5, 1),
        payload_json="not-json", source="tutor",
    ))
    db.add(PetTimelineEvent(
        pet_id="p1", tenant_id="t1", event_type="weight",
        title="Peso sem kg", occurred_at=datetime(2026, 5, 2),
        payload_json=json.dumps({"outra_chave": 10}), source="tutor",
    ))
    db.add(PetTimelineEvent(
        pet_id="p1", tenant_id="t1", event_type="weight",
        title="Peso ok", occurred_at=datetime(2026, 5, 3),
        payload_json=json.dumps({"kg": 9.0}), source="tutor",
    ))
    db.commit()

    c = _client(db, db.get(User, "u1"), monkeypatch=monkeypatch)
    r = c.get("/api/pets/p1/stats")
    assert r.status_code == 200
    series = r.json()["weight_series"]
    # Só o válido aparece
    assert len(series) == 1
    assert series[0]["kg"] == 9.0


def test_stats_walks_per_week_counts_completed(monkeypatch):
    """Walks com status completed são contados por semana."""
    db = _ctx()
    now = datetime.utcnow()
    # Walk desta semana, status completed
    db.add(Walk(
        id="w1", tutor_id="u1", pet_id="p1", tenant_id="t1",
        scheduled_date=now.date().isoformat(),
        duration_minutes=30, price=50.0, status="completed",
        created_at=now,
    ))
    # Walk desta semana, status agendado (não deve contar)
    db.add(Walk(
        id="w2", tutor_id="u1", pet_id="p1", tenant_id="t1",
        scheduled_date=now.date().isoformat(),
        duration_minutes=30, price=50.0, status="Agendado",
        created_at=now,
    ))
    db.commit()

    c = _client(db, db.get(User, "u1"), monkeypatch=monkeypatch)
    r = c.get("/api/pets/p1/stats")
    assert r.status_code == 200
    weeks = r.json()["walks_per_week"]
    assert len(weeks) == 12
    # A última semana tem o walk completed
    last_week = weeks[-1]
    assert last_week["count"] == 1


def test_stats_observations_distribution(monkeypatch):
    """WalkObservation dos últimos 90d são agregados corretamente."""
    db = _ctx()
    now = datetime.utcnow()
    db.add(WalkObservation(
        walk_id="w_fake", pet_id="p1", tenant_id="t1", walker_user_id="walker1",
        mood="calm", energy="high", socialization="good",
        peed=True, pooped=True, incident=False, created_at=now,
    ))
    db.add(WalkObservation(
        walk_id="w_fake2", pet_id="p1", tenant_id="t1", walker_user_id="walker1",
        mood="happy", energy="normal", socialization="neutral",
        peed=False, pooped=False, incident=True, created_at=now,
    ))
    db.commit()

    c = _client(db, db.get(User, "u1"), monkeypatch=monkeypatch)
    r = c.get("/api/pets/p1/stats")
    assert r.status_code == 200
    obs = r.json()["observations"]
    assert obs["total"] == 2
    assert obs["mood"]["calm"] == 1
    assert obs["mood"]["happy"] == 1
    assert obs["mood"]["anxious"] == 0
    assert obs["energy"]["high"] == 1
    assert obs["energy"]["normal"] == 1
    assert obs["socialization"]["good"] == 1
    assert obs["peed_pct"] == 50.0
    assert obs["pooped_pct"] == 50.0
    assert obs["incidents"] == 1


def test_stats_old_observations_excluded(monkeypatch):
    """WalkObservations com mais de 90 dias são excluídas."""
    db = _ctx()
    old = datetime.utcnow() - timedelta(days=95)
    db.add(WalkObservation(
        walk_id="w_old", pet_id="p1", tenant_id="t1", walker_user_id="walker1",
        mood="anxious", energy="low", created_at=old,
    ))
    db.commit()

    c = _client(db, db.get(User, "u1"), monkeypatch=monkeypatch)
    r = c.get("/api/pets/p1/stats")
    assert r.status_code == 200
    obs = r.json()["observations"]
    assert obs["total"] == 0


# ---------------------------------------------------------------------------
# Gate e ownership
# ---------------------------------------------------------------------------

def test_stats_gate_off_returns_404(monkeypatch):
    """Feature profile OFF → 404."""
    db = _ctx(profile_active=False)
    c = _client(db, db.get(User, "u1"), env_on=False, monkeypatch=monkeypatch)

    r = c.get("/api/pets/p1/stats")
    assert r.status_code == 404


def test_stats_non_owner_returns_404(monkeypatch):
    """User não-dono → 404."""
    db = _ctx()
    c = _client(db, db.get(User, "u2"), monkeypatch=monkeypatch)

    r = c.get("/api/pets/p1/stats")
    assert r.status_code == 404
