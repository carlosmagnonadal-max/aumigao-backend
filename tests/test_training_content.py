"""S2 — leitura do bundle JSON da Capacitação (versão ativa, cache, tolerância)."""
import json
import os

from app.services import training_content
from tests.training_helpers import make_bundle, write_bundle


def test_active_version_picks_highest_numeric(tmp_path, monkeypatch):
    monkeypatch.setenv("WALKER_TRAINING_CONTENT_DIR", str(tmp_path))
    monkeypatch.delenv("WALKER_TRAINING_REQUIRED_VERSION", raising=False)
    write_bundle(tmp_path, make_bundle(version="0.9"))
    write_bundle(tmp_path, make_bundle(version="0.10"))
    (tmp_path / "manual-lixo.json").write_text("{}", encoding="utf-8")
    assert training_content.available_versions() == ["0.9", "0.10"]
    assert training_content.active_version() == "0.10"


def test_required_version_env_overrides_latest(tmp_path, monkeypatch):
    monkeypatch.setenv("WALKER_TRAINING_CONTENT_DIR", str(tmp_path))
    write_bundle(tmp_path, make_bundle(version="1.0"))
    write_bundle(tmp_path, make_bundle(version="2.0"))
    monkeypatch.setenv("WALKER_TRAINING_REQUIRED_VERSION", "1.0")
    assert training_content.active_version() == "1.0"
    assert training_content.load_bundle()["version"] == "1.0"


def test_invalid_or_missing_version_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("WALKER_TRAINING_CONTENT_DIR", str(tmp_path))
    monkeypatch.setenv("WALKER_TRAINING_REQUIRED_VERSION", "../segredo")
    assert training_content.load_bundle() is None
    monkeypatch.setenv("WALKER_TRAINING_REQUIRED_VERSION", "3.0")
    assert training_content.load_bundle() is None


def test_missing_dir_means_no_bundle(tmp_path, monkeypatch):
    monkeypatch.setenv("WALKER_TRAINING_CONTENT_DIR", str(tmp_path / "nao-existe"))
    monkeypatch.delenv("WALKER_TRAINING_REQUIRED_VERSION", raising=False)
    assert training_content.active_version() is None
    assert training_content.load_bundle() is None


def test_invalid_json_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("WALKER_TRAINING_CONTENT_DIR", str(tmp_path))
    monkeypatch.setenv("WALKER_TRAINING_REQUIRED_VERSION", "4.0")
    (tmp_path / "manual-4.0.json").write_text("{quebrado", encoding="utf-8")
    assert training_content.load_bundle() is None


def test_cache_reloads_when_file_changes(tmp_path, monkeypatch):
    monkeypatch.setenv("WALKER_TRAINING_CONTENT_DIR", str(tmp_path))
    monkeypatch.setenv("WALKER_TRAINING_REQUIRED_VERSION", "9.0")
    path = write_bundle(tmp_path, make_bundle(status="draft"))
    assert training_content.load_bundle()["status"] == "draft"
    path.write_text(json.dumps(make_bundle(status="vet_approved")), encoding="utf-8")
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10**9))
    assert training_content.load_bundle()["status"] == "vet_approved"


def test_module_helpers():
    bundle = make_bundle()
    assert training_content.get_module(bundle, "m08")["title"] == "Primeiros socorros"
    assert training_content.get_module(bundle, "nao-existe") is None
    assert [m["id"] for m in training_content.required_modules(bundle)] == ["m01", "m08"]
