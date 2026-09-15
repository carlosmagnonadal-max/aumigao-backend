"""Capacitação do passeador (S2): progresso, quiz corrigido no servidor e guia rápido.

GABARITO NUNCA SAI DAQUI: payloads públicos não carregam correct_index/explanation/source
das perguntas; a correção devolve só acerto por questão + explicação (DV8).
Aprovação: acertos*100 >= 80*total (inteiro, sem arredondamento).
Conclusão: todos os módulos NACIONAIS aprovados na versão ativa (DV1) →
walker_profiles.training_completed_version/at.
"""
from __future__ import annotations

import importlib
import json
import logging
import re
import unicodedata
from datetime import datetime
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.walker_profile import WalkerProfile
from app.models.walker_training_progress import WalkerTrainingProgress
from app.services import training_content
from app.services.walker_training_policy import (
    TrainingEnforcement,
    get_enforcement,
    is_walker_trained,
    training_status_label,
)

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


def _review_sections_for(module: dict, quiz: list[dict], wrong_indexes: list[int]) -> list[str]:
    """Títulos das seções ("## ...") relacionadas às perguntas erradas (C1).

    Sem mapeamento pergunta→seção nos dados do bundle (nenhuma pergunta traz
    `section_id`) → devolve os títulos de TODAS as seções do módulo (fallback
    do contrato). Com mapeamento, devolve só as seções das perguntas erradas
    (deduplicado, na ordem das seções do módulo).
    """
    sections = module.get("sections") or []
    all_titles = [s["title"] for s in sections if s.get("title")]
    title_by_id = {s["id"]: s["title"] for s in sections if s.get("id") and s.get("title")}
    mapped: list[str] = []
    seen: set[str] = set()
    for index in wrong_indexes:
        section_id = quiz[index].get("section_id")
        title = title_by_id.get(section_id) if section_id else None
        if title and title not in seen:
            seen.add(title)
            mapped.append(title)
    return mapped if mapped else all_titles


def grade_quiz(
    db: Session, walker_user_id: str, module_id: str, answers: list[int], version: str | None = None,
) -> dict:
    bundle = _require_bundle()
    if version is not None and version != bundle["version"]:
        # C1: conteúdo mudou de versão entre o passeador abrir o módulo e enviar o quiz.
        raise HTTPException(status_code=409, detail={"code": "training_version_changed"})
    module = _require_module(bundle, module_id)
    quiz = module.get("quiz", [])
    if not quiz:
        raise HTTPException(status_code=409, detail="Este módulo não tem quiz.")
    if len(answers) != len(quiz):
        raise HTTPException(status_code=422, detail=f"Envie {len(quiz)} respostas.")

    full_results: list[dict] = []
    wrong_indexes: list[int] = []
    hits = 0
    for index, (question, answer) in enumerate(zip(quiz, answers)):
        if not isinstance(answer, int) or not 0 <= answer < len(question["options"]):
            raise HTTPException(status_code=422, detail=f"Resposta inválida na pergunta {index + 1}.")
        correct = answer == question["correct_index"]
        hits += int(correct)
        if not correct:
            wrong_indexes.append(index)
        full_results.append({"index": index, "correct": correct, "explanation": question.get("explanation", "")})

    total = len(quiz)
    score = round(hits * 100 / total)
    passed = hits * 100 >= PASS_THRESHOLD_PERCENT * total
    content_version = bundle["version"]
    now = datetime.utcnow()

    row = _progress_rows(db, walker_user_id, content_version).get(module_id)
    if row is None:
        row = WalkerTrainingProgress(
            id=str(uuid4()), walker_user_id=walker_user_id, content_version=content_version,
            module_id=module_id, best_score=0, attempts=0, created_at=now,
        )
        db.add(row)
        try:
            db.flush()
        except IntegrityError:
            # S2-4: envio simultâneo do 1º progresso — outra requisição já criou a
            # linha (uq_walker_training_progress_module); relê e segue com UPDATE
            # em vez de derrubar a requisição com 500.
            db.rollback()
            row = _progress_rows(db, walker_user_id, content_version).get(module_id)
            if row is None:
                raise
    row.attempts = (row.attempts or 0) + 1
    row.best_score = max(row.best_score or 0, score)
    if passed and row.passed_at is None:
        row.passed_at = now
    row.updated_at = now
    db.flush()

    training_completed = _maybe_complete_training(db, walker_user_id, bundle, now)
    db.commit()

    # C1: REPROVADO nunca devolve acerto por pergunta, explicação nem gabarito —
    # só os títulos das seções pra revisar. APROVADO segue como hoje (DV8).
    if passed:
        results, review_sections = full_results, []
    else:
        results, review_sections = [], _review_sections_for(module, quiz, wrong_indexes)

    return {
        "module_id": module_id,
        "score": score,
        "hits": hits,
        "total": total,
        "passed": passed,
        "pass_threshold": PASS_THRESHOLD_PERCENT,
        "results": results,
        "review_sections": review_sections,
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


# ── M10-B: Regras da sua cidade ───────────────────────────────────────────────


def _normalize(value: str | None) -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode("ascii")
    return " ".join(text.lower().split())


def _walker_location(profile: WalkerProfile | None) -> tuple[str, str]:
    """(cidade, UF). `state` do cadastro guarda a UF; valores que não são UF (bairro legado) viram ""."""
    if profile is None:
        return "", ""
    city = (profile.city or "").strip()
    raw_state = (profile.state or "").strip().upper()
    uf = raw_state if len(raw_state) == 2 and raw_state.isalpha() else ""
    return city, uf


def _local_module_matches(module: dict, city: str, uf: str) -> bool:
    module_uf = (module.get("uf") or "").upper()
    if uf and module_uf and uf != module_uf:
        return False
    normalized_city = _normalize(city)
    normalized_module_city = _normalize(module.get("cidade"))
    if not normalized_city or not normalized_module_city:
        return False
    return re.search(rf"\b{re.escape(normalized_city)}\b", normalized_module_city) is not None


def _json_object(value) -> dict:
    try:
        data = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _join_items(items: list[str]) -> str:
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " e " + items[-1]


_TEMA_LABELS = {
    "focinheira": "Focinheira",
    "guia": "Guia",
    "praia": "Praia",
    "dejetos": "Dejetos",
    "limite_caes": "Limite de cães",
}


def _tema_label(tema: str | None) -> str:
    return _TEMA_LABELS.get(tema or "", (tema or "Regra local").replace("_", " ").strip().capitalize())


def _conflict_summary(rule, items: list[str]) -> str:
    """status="conflito" nunca deve soar como permissão (C3) — texto de cautela, não de liberação."""
    label = _tema_label(getattr(rule, "tema", None))
    if getattr(rule, "tema", None) == "praia":
        action = f" se for, {_join_items(items)}." if items else ""
        return f"{label}: situação legal em conflito — evite levar o cão à praia até confirmação;{action}"
    action = f" se for, {_join_items(items)}." if items else " confirme antes de seguir."
    return f"{label}: situação legal em conflito — evite até confirmação;{action}"


def _serialize_local_rule(rule) -> dict:
    """LocalRule (ORM do S3) → dict público do M10-B. Só leitura de atributos (sem import do modelo)."""
    requirement = _json_object(getattr(rule, "exigencia_json", None))
    items = [str(item) for item in requirement.get("itens") or [] if str(item).strip()]
    status = getattr(rule, "status", None)
    if status == "conflito":
        # C3: status "conflito" -> resumo de cautela (nunca soa como permissão).
        summary = _conflict_summary(rule, items)
    else:
        summary = str(requirement.get("detalhe") or "").strip() or " ".join(
            part for part in (", ".join(items), str(requirement.get("onde") or "").strip()) if part
        )
    nota = str(requirement.get("nota") or "").strip() or None
    checked = getattr(rule, "verificado_em", None)
    return {
        "id": getattr(rule, "id", None),
        "tema": getattr(rule, "tema", None),
        "nivel": getattr(rule, "nivel", None),
        "municipio": getattr(rule, "municipio", None),
        "uf": getattr(rule, "uf", None),
        "status": status,
        "confianca": getattr(rule, "confianca", None),
        "norma": getattr(rule, "norma", None),
        "fonte_url": getattr(rule, "fonte_url", None),
        "verificado_em": checked.isoformat() if hasattr(checked, "isoformat") else checked,
        "itens": items,
        "resumo": summary,
        "nota": nota,
    }


def _structured_local_rules(db: Session, city: str, uf: str) -> list[dict]:
    """Adaptador do S3 — contrato do plano S3 (DV2): rules_for_city(db, municipio, uf) -> list[LocalRule].

    Chamada POSICIONAL (a assinatura do S3 é `rules_for_city(db, municipio, uf, *, include_tramitacao, cache)`).
    Devolve vigentes + "conflito" (o S3 marca praia de Salvador como conflito/baixa confiança);
    o app mostra o status. Sem o módulo, sem a função ou com erro → [] (Capacitação nunca quebra pelo S3).
    """
    if not city or not uf:
        return []
    try:
        service = importlib.import_module("app.services.local_rules_service")
    except ImportError:
        return []
    rules_for_city = getattr(service, "rules_for_city", None)
    if rules_for_city is None:
        return []
    try:
        return [_serialize_local_rule(rule) for rule in rules_for_city(db, city, uf)]
    except Exception as exc:  # noqa: BLE001 — fonte externa (S3); degrada para lista vazia
        logger.warning("training_local_rules_failed city=%s uf=%s reason=%s", city, uf, type(exc).__name__)
        db.rollback()
        return []


def local_rules(db: Session, walker_user_id: str) -> dict:
    profile = _profile(db, walker_user_id)
    city, uf = _walker_location(profile)
    bundle = training_content.load_bundle()
    modules: list[dict] = []
    if bundle is not None:
        rows = _progress_rows(db, walker_user_id, bundle["version"])
        modules = [
            _module_summary(module, rows.get(module["id"]))
            for module in bundle.get("modules", [])
            if module.get("scope") == "local" and _local_module_matches(module, city, uf)
        ]
    return {
        "city": city or None,
        "uf": uf or None,
        "modules": modules,
        "rules": _structured_local_rules(db, city, uf),
    }


# ── Admin ─────────────────────────────────────────────────────────────────────


def admin_training_detail(db: Session, walker_user_id: str) -> dict:
    profile = _profile(db, walker_user_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="Passeador não encontrado.")
    enforcement = get_enforcement()
    bundle = training_content.load_bundle(enforcement.required_version)
    version = bundle["version"] if bundle else None
    rows = _progress_rows(db, walker_user_id, version) if version else {}
    modules = [_module_summary(m, rows.get(m["id"])) for m in (bundle.get("modules", []) if bundle else [])]
    return {
        "user_id": walker_user_id,
        "version": version,
        "content_status": bundle.get("status") if bundle else None,
        "status": training_status_label(profile, version),
        "completed_version": profile.training_completed_version,
        "completed_at": profile.training_completed_at,
        "enforcement": _enforcement_payload(enforcement),
        "modules": modules,
    }
