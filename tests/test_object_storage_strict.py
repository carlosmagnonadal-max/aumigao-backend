"""Blindagem do storage de uploads (incidente 11/07 — logo do white label).

Em PRODUÇÃO, upload sem R2/GCS configurado deve FALHAR ALTO
(StorageNotConfiguredError → 503 na rota) em vez de cair no fallback de disco
local: o filesystem do Cloud Run é efêmero e o arquivo evapora no deploy
seguinte (perda silenciosa — o logo subiu durante a janela sem env vars e o
serve passou a responder 404).

Fora de produção (dev/testes) o fallback local continua valendo — dezenas de
testes e o fluxo local dependem dele.
"""
from __future__ import annotations

import pytest

from app.services import object_storage


def _clear_r2(monkeypatch):
    for k in ("R2_BUCKET", "R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        monkeypatch.delenv(k, raising=False)


def test_save_producao_sem_r2_falha_alto(monkeypatch, tmp_path):
    _clear_r2(monkeypatch)
    monkeypatch.setenv("ENVIRONMENT", "production")
    dest = tmp_path / "uploads" / "x.png"
    with pytest.raises(object_storage.StorageNotConfiguredError):
        object_storage.save(dest, b"abc", "image/png")
    assert not dest.exists()


def test_save_dev_sem_r2_mantem_fallback_local(monkeypatch, tmp_path):
    _clear_r2(monkeypatch)
    monkeypatch.setenv("ENVIRONMENT", "development")
    dest = tmp_path / "uploads" / "x.png"
    object_storage.save(dest, b"abc", "image/png")
    assert dest.read_bytes() == b"abc"


def test_save_env_ausente_mantem_fallback_local(monkeypatch, tmp_path):
    """ENVIRONMENT não setado (testes/dev antigos) = comportamento legado."""
    _clear_r2(monkeypatch)
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    dest = tmp_path / "uploads" / "y.png"
    object_storage.save(dest, b"xyz", "image/png")
    assert dest.read_bytes() == b"xyz"
