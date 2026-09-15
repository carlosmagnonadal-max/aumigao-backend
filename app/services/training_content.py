"""Leitura do bundle JSON da Capacitação do passeador (S2).

Gerado por scripts/build_training_bundle.py em app/content/training/manual-<versao>.json.
Versão ativa = WALKER_TRAINING_REQUIRED_VERSION ou a maior versão disponível.
Tolerante: diretório/arquivo ausente ou JSON inválido → None (nunca derruba request).
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_DIR = Path(__file__).resolve().parent.parent / "content" / "training"
_FILE_RE = re.compile(r"^manual-(\d+(?:\.\d+)*)\.json$")
_VERSION_RE = re.compile(r"^\d+(?:\.\d+)*$")
_cache: dict[tuple[str, int], dict] = {}


def content_dir() -> Path:
    override = os.getenv("WALKER_TRAINING_CONTENT_DIR", "").strip()
    return Path(override) if override else _DEFAULT_DIR


def _version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def available_versions() -> list[str]:
    directory = content_dir()
    if not directory.is_dir():
        return []
    versions = [m.group(1) for p in directory.iterdir() if (m := _FILE_RE.match(p.name))]
    return sorted(versions, key=_version_key)


def active_version() -> str | None:
    required = os.getenv("WALKER_TRAINING_REQUIRED_VERSION", "").strip()
    if required:
        return required
    versions = available_versions()
    return versions[-1] if versions else None


def load_bundle(version: str | None = None) -> dict | None:
    version = version or active_version()
    if not version or not _VERSION_RE.match(version):
        return None
    path = content_dir() / f"manual-{version}.json"
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        logger.warning("training_bundle_missing version=%s", version)
        return None
    key = (str(path), mtime)
    bundle = _cache.get(key)
    if bundle is None:
        try:
            bundle = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("training_bundle_invalid version=%s reason=%s", version, type(exc).__name__)
            return None
        _cache[key] = bundle
    return bundle


def get_module(bundle: dict, module_id: str) -> dict | None:
    return next((m for m in bundle.get("modules", []) if m.get("id") == module_id), None)


def required_modules(bundle: dict) -> list[dict]:
    """Só módulos nacionais contam para concluir a Capacitação (DV1)."""
    return [m for m in bundle.get("modules", []) if (m.get("scope") or "national") == "national"]
