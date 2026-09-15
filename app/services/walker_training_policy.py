"""Política da trava da Capacitação do passeador (S2).

Config (env, relida a cada chamada — monkeypatch-friendly):
  WALKER_TRAINING_ENFORCEMENT      off|warn|on (default off = zero regressão)
  WALKER_TRAINING_ENFORCED_FROM    AAAA-MM-DD (início da trava)
  WALKER_TRAINING_GRACE_DAYS       int (default 7; carência GLOBAL a partir do início)
  WALKER_TRAINING_REQUIRED_VERSION (default: maior bundle disponível)

Só BLOQUEIA (effective_mode="on") com: mode=on + bundle existe + status vet_approved
+ ENFORCED_FROM válido + agora >= ENFORCED_FROM + GRACE_DAYS. Qualquer falta → "warn"
+ log (D2: conteúdo sem assinatura de veterinário nunca bloqueia).
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from app.services import training_content

logger = logging.getLogger(__name__)

VALID_MODES = {"off", "warn", "on"}
DEFAULT_GRACE_DAYS = 7
_logged_reasons: set[str] = set()


@dataclass(frozen=True)
class TrainingEnforcement:
    configured_mode: str
    effective_mode: str
    required_version: str | None
    content_status: str | None
    deadline: datetime | None
    reason: str

    @property
    def blocking(self) -> bool:
        return self.effective_mode == "on"


def _log_once(reason: str, **fields) -> None:
    """Loga cada motivo de degradação 1x por processo (o matching chama isto a cada ranking)."""
    if reason in _logged_reasons:
        return
    _logged_reasons.add(reason)
    details = " ".join(f"{key}={value}" for key, value in fields.items())
    logger.warning("training_gate_degraded reason=%s %s", reason, details)


def _configured_mode() -> str:
    raw = os.getenv("WALKER_TRAINING_ENFORCEMENT", "off").strip().lower()
    if raw not in VALID_MODES:
        _log_once("invalid_mode", value=raw)
        return "off"
    return raw


def _grace_days() -> int:
    raw = os.getenv("WALKER_TRAINING_GRACE_DAYS", str(DEFAULT_GRACE_DAYS)).strip()
    try:
        return max(0, int(raw))
    except ValueError:
        _log_once("invalid_grace_days", value=raw)
        return DEFAULT_GRACE_DAYS


def _enforced_from() -> date | None:
    raw = os.getenv("WALKER_TRAINING_ENFORCED_FROM", "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        _log_once("invalid_enforced_from", value=raw)
        return None


def get_enforcement(now: datetime | None = None) -> TrainingEnforcement:
    now = now or datetime.utcnow()
    mode = _configured_mode()
    version = training_content.active_version()
    if mode == "off":
        return TrainingEnforcement(mode, "off", version, None, None, "mode_off")

    bundle = training_content.load_bundle(version)
    status = bundle.get("status") if bundle else None
    start = _enforced_from()
    deadline = datetime.combine(start, time.min) + timedelta(days=_grace_days()) if start else None

    if mode == "warn":
        return TrainingEnforcement(mode, "warn", version, status, deadline, "mode_warn")
    if bundle is None:
        _log_once("no_bundle", version=version)
        return TrainingEnforcement(mode, "warn", version, None, deadline, "no_bundle")
    if status != "vet_approved":
        _log_once("draft_content", version=version, status=status)
        return TrainingEnforcement(mode, "warn", version, status, deadline, "draft_content")
    if deadline is None:
        _log_once("missing_enforced_from", version=version)
        return TrainingEnforcement(mode, "warn", version, status, None, "missing_enforced_from")
    if now < deadline:
        return TrainingEnforcement(mode, "warn", version, status, deadline, "grace_period")
    return TrainingEnforcement(mode, "on", version, status, deadline, "enforced")


def is_walker_trained(profile, required_version: str | None) -> bool:
    if profile is None or not required_version:
        return False
    return (getattr(profile, "training_completed_version", None) or "") == required_version


def training_status_label(profile, required_version: str | None) -> str:
    """completed | pending | outdated | unavailable (sem bundle ativo)."""
    if not required_version:
        return "unavailable"
    completed = getattr(profile, "training_completed_version", None) if profile is not None else None
    if not completed:
        return "pending"
    return "completed" if completed == required_version else "outdated"
