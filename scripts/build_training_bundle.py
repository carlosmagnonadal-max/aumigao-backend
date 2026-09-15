#!/usr/bin/env python
"""Gera o bundle JSON da Capacitação do passeador (S2) a partir do manual em markdown.

Fonte única: docs/manual-passeador/modulos/NN[letra]-slug.md (repo raiz, fora do backend).
Saída: backend/app/content/training/manual-<versao>.json. O runtime (app/services/
training_content.py) só lê esse JSON — nunca markdown/YAML.

- Exclui a seção "Para o veterinário revisar".
- Remove as marcações "⚠️ VERIFICAR-*" do texto do passeador (conta quantas havia).
- --status vet_approved exige assinatura (nome, CRMV, data) e ZERO marcações pendentes.
- Módulo com `uf` no front-matter = scope "local" (M10-B); sem `uf` = "national".

Uso (na pasta backend/):
    venv/Scripts/python.exe scripts/build_training_bundle.py --versao 0.2
    venv/Scripts/python.exe scripts/build_training_bundle.py --versao 1.0 --status vet_approved \
        --vet-nome "Nome" --vet-crmv "CRMV-BA 0000" --vet-data 2026-10-01
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

BACKEND_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SOURCE_DIR = BACKEND_ROOT.parent / "docs" / "manual-passeador" / "modulos"
DEFAULT_OUTPUT_DIR = BACKEND_ROOT / "app" / "content" / "training"

MODULE_FILE_RE = re.compile(r"^\d{2}[a-z]?-[a-z0-9-]+\.md$")
VERSION_RE = re.compile(r"^\d+(?:\.\d+)*$")
REVIEW_MARKER_RE = re.compile(r"[ \t]*⚠️?[ \t]*\**VERIFICAR-[^\n]*")
EMPTY_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*]|\d+\.)\s*$")
SECTION_ID_RE = re.compile(r"^(\d+[A-Za-z]?(?:\.\d+)*)\.?\s+(.+)$")
SOURCE_LINE_RE = re.compile(r"^\s*-\s*\*\*\[(F\d+)\]\*\*\s*(.+)$")
QUIZ_FENCE_RE = re.compile(r"```ya?ml[ \t]*\n(.*?)\n```", re.DOTALL)

H2_QUICK_CARD = "cartão rápido".casefold()
H2_QUIZ = "quiz"
H2_SOURCES = "fontes"
H2_VET_REVIEW = "para o veterinário revisar".casefold()
STATUSES = ("draft", "vet_approved")
QUICK_GUIDE_MODULES = ["m08", "m09"]
PASS_THRESHOLD = 80


class BundleError(ValueError):
    """Erro de formato do manual — a mensagem aponta o arquivo/trecho."""


def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n").lstrip("﻿")


def count_review_markers(text: str) -> int:
    return len(REVIEW_MARKER_RE.findall(text))


def strip_review_markers(text: str) -> str:
    return REVIEW_MARKER_RE.sub("", text).strip()


def clean_markdown(markdown: str) -> str:
    """Remove marcações VERIFICAR, itens de lista que ficaram vazios e bordas em branco/---."""
    lines: list[str] = []
    for line in _normalize_newlines(markdown).split("\n"):
        had_marker = bool(REVIEW_MARKER_RE.search(line))
        line = REVIEW_MARKER_RE.sub("", line).rstrip()
        if had_marker and (not line.strip() or EMPTY_LIST_ITEM_RE.match(line) or line.strip() == ">"):
            continue
        lines.append(line)
    while lines and (not lines[-1].strip() or lines[-1].strip() == "---"):
        lines.pop()
    while lines and not lines[0].strip():
        lines.pop(0)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines))


def split_front_matter(text: str, file_name: str) -> tuple[dict, str]:
    lines = _normalize_newlines(text).split("\n")
    if not lines or lines[0].strip() != "---":
        raise BundleError(f"{file_name}: front-matter YAML ausente (primeira linha deve ser ---)")
    for end in range(1, len(lines)):
        if lines[end].strip() == "---":
            meta = yaml.safe_load("\n".join(lines[1:end])) or {}
            if not isinstance(meta, dict):
                raise BundleError(f"{file_name}: front-matter inválido")
            return meta, "\n".join(lines[end + 1:])
    raise BundleError(f"{file_name}: front-matter sem fechamento ---")


def split_h2_sections(body: str) -> tuple[str, list[tuple[str, str]]]:
    """Separa a abertura (sem o título #) e as seções ## (ignora ## dentro de ```)."""
    intro: list[str] = []
    sections: list[tuple[str, list[str]]] = []
    in_fence = False
    for line in body.split("\n"):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        if not in_fence and line.startswith("## "):
            sections.append((line[3:].strip(), []))
            continue
        (sections[-1][1] if sections else intro).append(line)
    intro_text = "\n".join(line for line in intro if not line.startswith("# "))
    return intro_text, [(title, "\n".join(body_lines)) for title, body_lines in sections]


def parse_quick_cards(markdown: str, module_title: str) -> list[dict]:
    """Cartão único (sem ###) vira 1 cartão com o título do módulo; com ### vira 1 por subtítulo (M08)."""
    head: list[str] = []
    cards: list[tuple[str, list[str]]] = []
    for line in markdown.split("\n"):
        if line.startswith("### "):
            cards.append((line[4:].strip(), []))
            continue
        (cards[-1][1] if cards else head).append(line)
    result: list[dict] = []
    head_md = clean_markdown("\n".join(head))
    if head_md:
        result.append({"title": module_title, "markdown": head_md})
    for title, card_lines in cards:
        card_md = clean_markdown("\n".join(card_lines))
        if card_md:
            result.append({"title": strip_review_markers(title), "markdown": card_md})
    return result


def parse_quiz(markdown: str, module_id: str) -> list[dict]:
    match = QUIZ_FENCE_RE.search(markdown)
    if not match:
        raise BundleError(f"{module_id}: bloco yaml do Quiz ausente")
    try:
        items = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        raise BundleError(f"{module_id}: YAML do Quiz inválido ({exc})") from exc
    if not isinstance(items, list) or not items:
        raise BundleError(f"{module_id}: Quiz vazio")
    quiz: list[dict] = []
    for number, item in enumerate(items, start=1):
        where = f"{module_id} pergunta {number}"
        if not isinstance(item, dict):
            raise BundleError(f"{where}: item não é um mapa")
        question = item.get("pergunta")
        options = item.get("opcoes")
        correct = item.get("correta")
        if not isinstance(question, str) or not question.strip():
            raise BundleError(f"{where}: 'pergunta' vazia")
        if not isinstance(options, list) or len(options) < 2 or not all(isinstance(o, str) and o.strip() for o in options):
            raise BundleError(f"{where}: 'opcoes' precisa de 2 ou mais textos")
        if isinstance(correct, bool) or not isinstance(correct, int) or not 0 <= correct < len(options):
            raise BundleError(f"{where}: 'correta' fora do intervalo 0..{len(options) - 1}")
        source = item.get("fonte")
        quiz.append({
            "question": strip_review_markers(question),
            "options": [strip_review_markers(option) for option in options],
            "correct_index": correct,
            "explanation": strip_review_markers(str(item.get("explicacao") or "")),
            "source": None if source is None else (strip_review_markers(str(source)) or None),
        })
    return quiz


def parse_sources(markdown: str) -> list[dict]:
    sources: list[dict] = []
    for line in markdown.split("\n"):
        match = SOURCE_LINE_RE.match(line)
        if match:
            sources.append({"id": match.group(1), "text": strip_review_markers(match.group(2))})
    return sources


def parse_sections(sections: list[tuple[str, str]]) -> list[dict]:
    result: list[dict] = []
    for index, (title, markdown) in enumerate(sections, start=1):
        match = SECTION_ID_RE.match(title)
        section_id, section_title = (match.group(1), match.group(2)) if match else (str(index), title)
        result.append({
            "id": section_id,
            "title": strip_review_markers(section_title),
            "markdown": clean_markdown(markdown),
        })
    return result


def build_module(path: Path, order: int) -> dict:
    meta, body = split_front_matter(path.read_text(encoding="utf-8"), path.name)
    module_id = str(meta.get("id") or "").strip()
    title = str(meta.get("titulo") or "").strip()
    if not module_id or not title:
        raise BundleError(f"{path.name}: front-matter precisa de 'id' e 'titulo'")
    intro, h2_sections = split_h2_sections(body)
    content_sections: list[tuple[str, str]] = []
    quick_md: str | None = None
    quiz_md: str | None = None
    sources_md = ""
    reviewable = [intro]
    for h2_title, markdown in h2_sections:
        key = h2_title.strip().casefold()
        if key == H2_VET_REVIEW:
            continue
        reviewable.append(h2_title)
        reviewable.append(markdown)
        if key == H2_QUICK_CARD:
            quick_md = markdown
        elif key == H2_QUIZ:
            quiz_md = markdown
        elif key == H2_SOURCES:
            sources_md = markdown
        else:
            content_sections.append((h2_title, markdown))
    if quiz_md is None:
        raise BundleError(f"{path.name}: seção '## Quiz' ausente")
    uf = str(meta["uf"]).strip().upper() if meta.get("uf") else None
    return {
        "id": module_id,
        "order": order,
        "file": path.name,
        "title": title,
        "source_version": str(meta.get("versao") or ""),
        "reading_minutes": int(meta["tempo_leitura_min"]) if meta.get("tempo_leitura_min") else None,
        "objectives": [strip_review_markers(str(o)) for o in (meta.get("objetivos") or [])],
        "scope": "local" if uf else "national",
        "uf": uf,
        "cidade": str(meta["cidade"]).strip() if meta.get("cidade") else None,
        "intro_markdown": clean_markdown(intro),
        "sections": parse_sections(content_sections),
        "quick_cards": parse_quick_cards(quick_md, title) if quick_md else [],
        "quiz": parse_quiz(quiz_md, module_id),
        "sources": parse_sources(sources_md),
        "review_markers": count_review_markers("\n".join(reviewable)),
    }


def build_bundle(
    source_dir: Path,
    version: str,
    status: str = "draft",
    vet_signature: dict | None = None,
    built_at: str | None = None,
) -> dict:
    if not VERSION_RE.match(version):
        raise BundleError(f"versão inválida '{version}' (use números separados por ponto, ex.: 0.2)")
    if status not in STATUSES:
        raise BundleError(f"status inválido '{status}' (use {', '.join(STATUSES)})")
    files = sorted(p for p in Path(source_dir).iterdir() if p.is_file() and MODULE_FILE_RE.match(p.name))
    if not files:
        raise BundleError(f"nenhum módulo NN-slug.md em {source_dir}")
    modules = [build_module(path, order) for order, path in enumerate(files, start=1)]
    ids = [m["id"] for m in modules]
    duplicated = sorted({i for i in ids if ids.count(i) > 1})
    if duplicated:
        raise BundleError(f"ids de módulo duplicados: {', '.join(duplicated)}")
    if status == "vet_approved":
        if not vet_signature or not all(str(vet_signature.get(k) or "").strip() for k in ("nome", "crmv", "data")):
            raise BundleError("vet_approved exige --vet-nome, --vet-crmv e --vet-data")
        pending = [f"{m['id']}={m['review_markers']}" for m in modules if m["review_markers"]]
        if pending:
            raise BundleError(f"vet_approved com marcações VERIFICAR pendentes: {', '.join(pending)}")
    return {
        "schema": 1,
        "version": version,
        "status": status,
        "vet_signature": vet_signature if status == "vet_approved" else None,
        "built_at": built_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "pass_threshold": PASS_THRESHOLD,
        "quick_guide_modules": [m for m in QUICK_GUIDE_MODULES if m in ids],
        "modules": modules,
    }


def write_bundle(bundle: dict, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"manual-{bundle['version']}.json"
    path.write_text(json.dumps(bundle, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--versao", required=True)
    parser.add_argument("--status", default="draft", choices=STATUSES)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE_DIR)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--vet-nome")
    parser.add_argument("--vet-crmv")
    parser.add_argument("--vet-data")
    args = parser.parse_args(argv)
    signature = None
    if args.status == "vet_approved":
        signature = {"nome": args.vet_nome, "crmv": args.vet_crmv, "data": args.vet_data}
    try:
        bundle = build_bundle(args.source, args.versao, args.status, signature)
    except BundleError as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        return 2
    path = write_bundle(bundle, args.output)
    questions = sum(len(m["quiz"]) for m in bundle["modules"])
    markers = sum(m["review_markers"] for m in bundle["modules"])
    print(
        f"bundle {path.name}: {len(bundle['modules'])} modulos, {questions} perguntas, "
        f"status={bundle['status']}, marcacoes VERIFICAR removidas={markers}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
