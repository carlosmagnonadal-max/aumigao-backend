"""Capacitação do passeador (S2): progresso, quiz corrigido no servidor e guia rápido.

GABARITO NUNCA SAI DAQUI: payloads públicos não carregam correct_index/explanation/source
das perguntas; a correção devolve só acerto por questão + explicação (DV8).
Aprovação: acertos*100 >= 80*total (inteiro, sem arredondamento).
Conclusão: todos os módulos NACIONAIS aprovados na versão ativa (DV1) →
walker_profiles.training_completed_version/at.
"""
from __future__ import annotations

import logging
from datetime import datetime
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.walker_profile import WalkerProfile
from app.models.walker_training_progress import WalkerTrainingProgress
from app.services import training_content
from app.services.walker_training_policy import TrainingEnforcement, get_enforcement, is_walker_trained

logger = logging.getLogger(__name__)

PASS_THRESHOLD_PERCENT = 80


def _profile(db: Session, walker_user_id: str) -> WalkerProfile | None:
    return db.query(WalkerProfile).filter(WalkerProfile.user_id == walker_user_id).first()


def _progress_rows(db: Session, walker_user_id: str, version: str) -> dict[str, WalkerTrainingProgress]:
    rows = (
        db.query(WalkerTrainingProgress)
        .filter(
            WalkerTrainingProgress.walker_user_id == walker_user_id,
            WalkerTrainingProgress.content_version == version,
        )
        .all()
    )
    return {row.module_id: row for row in rows}


def _progress_payload(row: WalkerTrainingProgress | None) -> dict:
    if row is None:
        return {"passed": False, "best_score": 0, "attempts": 0, "passed_at": None}
    return {
        "passed": row.passed_at is not None,
        "best_score": row.best_score or 0,
        "attempts": row.attempts or 0,
        "passed_at": row.passed_at,
    }


def _module_summary(module: dict, row: WalkerTrainingProgress | None) -> dict:
    return {
        "id": module["id"],
        "order": module.get("order"),
        "title": module.get("title"),
        "reading_minutes": module.get("reading_minutes"),
        "objectives": module.get("objectives", []),
        "scope": module.get("scope") or "national",
        "uf": module.get("uf"),
        "cidade": module.get("cidade"),
        "question_count": len(module.get("quiz", [])),
        **_progress_payload(row),
    }


def _enforcement_payload(enforcement: TrainingEnforcement) -> dict:
    return {
        "mode": enforcement.effective_mode,
        "blocking": enforcement.blocking,
        "deadline": enforcement.deadline,
    }


def _require_bundle() -> dict:
    bundle = training_content.load_bundle()
    if bundle is None:
        raise HTTPException(status_code=404, detail="Capacitação indisponível no momento.")
    return bundle


def _require_module(bundle: dict, module_id: str) -> dict:
    module = training_content.get_module(bundle, module_id)
    if module is None:
        raise HTTPException(status_code=404, detail="Módulo não encontrado.")
    return module


def training_summary(db: Session, walker_user_id: str) -> dict:
    enforcement = get_enforcement()
    bundle = training_content.load_bundle(enforcement.required_version)
    if bundle is None:
        return {
            "available": False,
            "version": None,
            "content_status": None,
            "vet_signature": None,
            "pass_threshold": PASS_THRESHOLD_PERCENT,
            "enforcement": _enforcement_payload(enforcement),
            "completed": False,
            "completed_at": None,
            "modules": [],
            "passed_count": 0,
            "total_modules": 0,
        }
    version = bundle["version"]
    rows = _progress_rows(db, walker_user_id, version)
    modules = [_module_summary(m, rows.get(m["id"])) for m in training_content.required_modules(bundle)]
    profile = _profile(db, walker_user_id)
    completed = is_walker_trained(profile, version)
    return {
        "available": True,
        "version": version,
        "content_status": bundle.get("status"),
        "vet_signature": bundle.get("vet_signature"),
        "pass_threshold": PASS_THRESHOLD_PERCENT,
        "enforcement": _enforcement_payload(enforcement),
        "completed": completed,
        "completed_at": profile.training_completed_at if completed and profile else None,
        "modules": modules,
        "passed_count": sum(1 for m in modules if m["passed"]),
        "total_modules": len(modules),
    }


def module_detail(db: Session, walker_user_id: str, module_id: str) -> dict:
    bundle = _require_bundle()
    module = _require_module(bundle, module_id)
    row = _progress_rows(db, walker_user_id, bundle["version"]).get(module_id)
    return {
        **_module_summary(module, row),
        "version": bundle["version"],
        "content_status": bundle.get("status"),
        "intro_markdown": module.get("intro_markdown", ""),
        "sections": module.get("sections", []),
        "quick_cards": module.get("quick_cards", []),
        "quiz": [
            {"index": index, "question": question["question"], "options": list(question["options"])}
            for index, question in enumerate(module.get("quiz", []))
        ],
        "sources": module.get("sources", []),
    }


def _maybe_complete_training(db: Session, walker_user_id: str, bundle: dict, now: datetime) -> bool:
    required_ids = {m["id"] for m in training_content.required_modules(bundle)}
    passed_ids = {
        module_id
        for module_id, row in _progress_rows(db, walker_user_id, bundle["version"]).items()
        if row.passed_at is not None
    }
    if not required_ids or not required_ids <= passed_ids:
        return False
    profile = _profile(db, walker_user_id)
    if profile is None:
        return False
    if profile.training_completed_version != bundle["version"]:
        profile.training_completed_version = bundle["version"]
        profile.training_completed_at = now
        logger.info("walker_training_completed user_id=%s version=%s", walker_user_id, bundle["version"])
    return True


def grade_quiz(db: Session, walker_user_id: str, module_id: str, answers: list[int]) -> dict:
    bundle = _require_bundle()
    module = _require_module(bundle, module_id)
    quiz = module.get("quiz", [])
    if not quiz:
        raise HTTPException(status_code=409, detail="Este módulo não tem quiz.")
    if len(answers) != len(quiz):
        raise HTTPException(status_code=422, detail=f"Envie {len(quiz)} respostas.")

    results: list[dict] = []
    hits = 0
    for index, (question, answer) in enumerate(zip(quiz, answers)):
        if not isinstance(answer, int) or not 0 <= answer < len(question["options"]):
            raise HTTPException(status_code=422, detail=f"Resposta inválida na pergunta {index + 1}.")
        correct = answer == question["correct_index"]
        hits += int(correct)
        results.append({"index": index, "correct": correct, "explanation": question.get("explanation", "")})

    total = len(quiz)
    score = round(hits * 100 / total)
    passed = hits * 100 >= PASS_THRESHOLD_PERCENT * total
    version = bundle["version"]
    now = datetime.utcnow()

    row = _progress_rows(db, walker_user_id, version).get(module_id)
    if row is None:
        row = WalkerTrainingProgress(
            id=str(uuid4()), walker_user_id=walker_user_id, content_version=version,
            module_id=module_id, best_score=0, attempts=0, created_at=now,
        )
        db.add(row)
    row.attempts = (row.attempts or 0) + 1
    row.best_score = max(row.best_score or 0, score)
    if passed and row.passed_at is None:
        row.passed_at = now
    row.updated_at = now
    db.flush()

    training_completed = _maybe_complete_training(db, walker_user_id, bundle, now)
    db.commit()
    return {
        "module_id": module_id,
        "score": score,
        "hits": hits,
        "total": total,
        "passed": passed,
        "pass_threshold": PASS_THRESHOLD_PERCENT,
        "results": results,
        "progress": _progress_payload(row),
        "training_completed": training_completed,
    }


def quick_guide() -> dict:
    """Cartões rápidos dos módulos de socorro (M08/M09) para cache offline no app."""
    bundle = _require_bundle()
    cards: list[dict] = []
    for module_id in bundle.get("quick_guide_modules") or ["m08", "m09"]:
        module = training_content.get_module(bundle, module_id)
        if module is None:
            continue
        for card in module.get("quick_cards", []):
            cards.append({
                "module_id": module_id,
                "module_title": module.get("title"),
                "title": card["title"],
                "markdown": card["markdown"],
            })
    return {"version": bundle["version"], "content_status": bundle.get("status"), "cards": cards}
