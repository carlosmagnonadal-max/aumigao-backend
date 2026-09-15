"""Helpers compartilhados dos testes da Capacitação (S2). Não é coletado (não começa com test_)."""
import json
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 — registra todas as tabelas
from app.core.database import Base
from app.models.user import User
from app.models.walker_profile import WalkerProfile

WALKER_ID = "walker-training"
PROFILE_ID = "wp-training"
TENANT_ID = "t-training"
VERSION = "9.0"

# Gabaritos do bundle de teste (para os testes montarem respostas certas).
M01_ANSWERS = [0, 1, 2, 3, 0]
M08_ANSWERS = [1] * 10
M10B_ANSWERS = [2]


def _question(n: int, correct: int) -> dict:
    return {
        "question": f"Pergunta {n}?",
        "options": ["A", "B", "C", "D"],
        "correct_index": correct,
        "explanation": f"Explicação {n}.",
        "source": "F1",
    }


def _module(module_id: str, order: int, title: str, answers: list[int], **extra) -> dict:
    base = {
        "id": module_id,
        "order": order,
        "file": f"{order:02d}-{module_id}.md",
        "title": title,
        "source_version": "0.1-rascunho",
        "reading_minutes": 10,
        "objectives": ["Objetivo"],
        "scope": "national",
        "uf": None,
        "cidade": None,
        "intro_markdown": "Abertura.",
        "sections": [{"id": f"{order}.1", "title": "Seção", "markdown": "> [!FACA] Faça certo."}],
        "quick_cards": [{"title": title, "markdown": "1. Passo."}],
        "quiz": [_question(i, correct) for i, correct in enumerate(answers)],
        "sources": [{"id": "F1", "text": "Fonte 1."}],
        "review_markers": 0,
    }
    base.update(extra)
    return base


def make_bundle(*, version: str = VERSION, status: str = "vet_approved") -> dict:
    return {
        "schema": 1,
        "version": version,
        "status": status,
        "vet_signature": (
            {"nome": "Dra. Teste", "crmv": "CRMV-BA 0000", "data": "2026-09-01"} if status == "vet_approved" else None
        ),
        "built_at": "2026-09-15T00:00:00Z",
        "pass_threshold": 80,
        "quick_guide_modules": ["m08"],
        "modules": [
            _module("m01", 1, "Passeio seguro", M01_ANSWERS),
            _module(
                "m08", 2, "Primeiros socorros", M08_ANSWERS,
                quick_cards=[
                    {"title": "8.4 Sangramento", "markdown": "1. Aperte."},
                    {"title": "8.7 Insolação", "markdown": "1. Resfrie."},
                ],
            ),
            _module(
                "m10b-salvador", 3, "Regras da sua cidade: Salvador (BA)", M10B_ANSWERS,
                scope="local", uf="BA", cidade="Salvador", quick_cards=[],
            ),
        ],
    }


def write_bundle(directory: Path, bundle: dict) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"manual-{bundle['version']}.json"
    path.write_text(json.dumps(bundle, ensure_ascii=False), encoding="utf-8")
    return path


def configure_training(
    monkeypatch,
    directory: Path,
    *,
    mode: str = "on",
    version: str = VERSION,
    enforced_from: str | None = "2000-01-01",
    grace_days: str = "0",
) -> None:
    monkeypatch.setenv("WALKER_TRAINING_CONTENT_DIR", str(directory))
    monkeypatch.setenv("WALKER_TRAINING_ENFORCEMENT", mode)
    monkeypatch.setenv("WALKER_TRAINING_REQUIRED_VERSION", version)
    monkeypatch.setenv("WALKER_TRAINING_GRACE_DAYS", grace_days)
    if enforced_from is None:
        monkeypatch.delenv("WALKER_TRAINING_ENFORCED_FROM", raising=False)
    else:
        monkeypatch.setenv("WALKER_TRAINING_ENFORCED_FROM", enforced_from)
    try:
        from app.services import walker_training_policy
        walker_training_policy._logged_reasons.clear()
    except ImportError:
        pass


def make_db(*, city: str = "Salvador", state: str = "BA", completed_version: str | None = None):
    """SQLite em memória com todas as tabelas + passeador ativo WALKER_ID (usar a partir da Task 5)."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    # E-mail/nomes sem tokens de "fake" (admin_serializers.FAKE_ENTITY_TOKENS: test, demo, local...),
    # senão a listagem do admin filtra o passeador.
    db.add(User(id=WALKER_ID, email="joao.passeador@aumigao.com.br", password_hash="x", role="walker",
                full_name="Joao Passeador", tenant_id=TENANT_ID, is_active=True))
    profile = WalkerProfile(id=PROFILE_ID, user_id=WALKER_ID, full_name="Joao Passeador", city=city,
                            state=state, status="active", active_as_walker=True)
    if completed_version is not None:
        profile.training_completed_version = completed_version
    db.add(profile)
    db.commit()
    return db
