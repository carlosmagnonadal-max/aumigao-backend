"""S2 — política da trava da Capacitação (D2: só bloqueia com vet_approved; carência)."""
import logging
from datetime import datetime
from types import SimpleNamespace

from app.services import walker_training_policy as policy
from tests.training_helpers import configure_training, make_bundle, write_bundle


def _setup(tmp_path, monkeypatch, *, status="vet_approved", **kwargs):
    write_bundle(tmp_path, make_bundle(status=status))
    configure_training(monkeypatch, tmp_path, **kwargs)


def test_default_is_off_and_never_blocks(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    monkeypatch.delenv("WALKER_TRAINING_ENFORCEMENT", raising=False)
    state = policy.get_enforcement()
    assert state.effective_mode == "off"
    assert state.blocking is False


def test_invalid_mode_falls_back_to_off(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, mode="banana")
    assert policy.get_enforcement().effective_mode == "off"


def test_warn_never_blocks(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, mode="warn")
    state = policy.get_enforcement()
    assert state.effective_mode == "warn"
    assert state.blocking is False


def test_on_with_vet_approved_after_deadline_blocks(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, mode="on")
    state = policy.get_enforcement()
    assert state.effective_mode == "on"
    assert state.blocking is True
    assert state.required_version == "9.0"
    assert state.reason == "enforced"


def test_on_with_draft_content_is_warn_and_logs(tmp_path, monkeypatch, caplog):
    _setup(tmp_path, monkeypatch, mode="on", status="draft")
    caplog.set_level(logging.WARNING, logger="app.services.walker_training_policy")
    state = policy.get_enforcement()
    assert state.effective_mode == "warn"
    assert state.blocking is False
    assert state.reason == "draft_content"
    assert "draft_content" in caplog.text


def test_on_without_enforced_from_is_warn(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, mode="on", enforced_from=None)
    state = policy.get_enforcement()
    assert state.blocking is False
    assert state.reason == "missing_enforced_from"


def test_on_without_bundle_is_warn(tmp_path, monkeypatch):
    configure_training(monkeypatch, tmp_path / "vazio", mode="on")
    state = policy.get_enforcement()
    assert state.blocking is False
    assert state.reason == "no_bundle"


def test_grace_period_respected(tmp_path, monkeypatch):
    # S2-5: a carência termina à meia-noite em America/Sao_Paulo (UTC-3, sem
    # horário de verão), ou seja, às 03:00 UTC do dia seguinte ao fim da carência.
    _setup(tmp_path, monkeypatch, mode="on", enforced_from="2026-09-15", grace_days="7")
    during = policy.get_enforcement(now=datetime(2026, 9, 21, 23, 59))
    assert during.blocking is False
    assert during.reason == "grace_period"
    assert during.deadline == datetime(2026, 9, 22, 3, 0)
    assert policy.get_enforcement(now=datetime(2026, 9, 22, 3, 0)).blocking is True


def test_grace_period_boundary_is_sao_paulo_midnight_not_utc_midnight(tmp_path, monkeypatch):
    # S2-5 (borda 21h-00h): meia-noite UTC do dia do fim da carência ainda é
    # 21h da véspera em Brasília — não pode ligar a trava nesse instante.
    _setup(tmp_path, monkeypatch, mode="on", enforced_from="2026-09-15", grace_days="0")
    assert policy.get_enforcement(now=datetime(2026, 9, 14, 23, 59)).blocking is False
    assert policy.get_enforcement(now=datetime(2026, 9, 15, 0, 0)).blocking is False  # 21h BRT (dia 14)
    assert policy.get_enforcement(now=datetime(2026, 9, 15, 2, 59)).blocking is False  # 23h59 BRT (dia 14)
    assert policy.get_enforcement(now=datetime(2026, 9, 15, 3, 0)).blocking is True  # 00h00 BRT (dia 15)


def test_invalid_grace_days_uses_7(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, mode="on", enforced_from="2026-09-15", grace_days="x")
    assert policy.get_enforcement(now=datetime(2026, 9, 21)).blocking is False
    assert policy.get_enforcement(now=datetime(2026, 9, 22, 3, 0)).blocking is True


def test_is_walker_trained_and_status_label():
    trained = SimpleNamespace(training_completed_version="9.0")
    old = SimpleNamespace(training_completed_version="8.0")
    never = SimpleNamespace(training_completed_version=None)
    assert policy.is_walker_trained(trained, "9.0") is True
    assert policy.is_walker_trained(old, "9.0") is False
    assert policy.is_walker_trained(None, "9.0") is False
    assert policy.is_walker_trained(trained, None) is False
    assert policy.training_status_label(trained, "9.0") == "completed"
    assert policy.training_status_label(old, "9.0") == "outdated"
    assert policy.training_status_label(never, "9.0") == "pending"
    assert policy.training_status_label(trained, None) == "unavailable"
