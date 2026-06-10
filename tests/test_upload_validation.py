"""Testes de unidade para app/services/upload_validation.py.

Cobertura:
- _looks_like_image: magic bytes JPEG / PNG / GIF / WEBP / HEIC vs lixo / bordas.
- read_image_upload_safely: limite de tamanho (MAX_UPLOAD_BYTES), arquivo vazio,
  validacao de magic bytes, leitura em chunks.
- enforce_upload_rate_limit: bloqueio por IP, contagem por origem, fallback de IP,
  expiracao da janela com clock injetado.

Testes contra o COMPORTAMENTO ATUAL do codigo.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from app.services import upload_validation as uv
from app.services.login_rate_limiter import InMemoryLoginRateLimiter


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
class _FakeUploadFile:
    """Imita o contrato usado por read_image_upload_safely: await file.read(n)."""

    def __init__(self, data: bytes):
        self._data = data
        self._pos = 0

    async def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            chunk = self._data[self._pos :]
            self._pos = len(self._data)
            return chunk
        chunk = self._data[self._pos : self._pos + size]
        self._pos += len(chunk)
        return chunk


class _FakeClient:
    def __init__(self, host):
        self.host = host


class _FakeRequest:
    def __init__(self, host="1.2.3.4"):
        self.client = _FakeClient(host) if host is not None else None


def _run(coro):
    return asyncio.run(coro)


# Magic byte prefixes reais aceitos pelo modulo.
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF"
PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
GIF87 = b"GIF87a\x01\x00\x01\x00"
GIF89 = b"GIF89a\x01\x00\x01\x00"
# WEBP: "RIFF" <4 bytes tamanho> "WEBP"
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 "
# HEIC: bytes 0-3 = tamanho da box, 4-7 = "ftyp", 8-11 = marca conhecida
HEIC = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00"


# ----------------------------------------------------------------------------
# _looks_like_image — caminhos felizes (magic bytes validos)
# ----------------------------------------------------------------------------
@pytest.mark.parametrize(
    "head",
    [JPEG, PNG, GIF87, GIF89, WEBP, HEIC],
    ids=["jpeg", "png", "gif87", "gif89", "webp", "heic"],
)
def test_looks_like_image_accepts_known_signatures(head):
    assert uv._looks_like_image(head) is True


@pytest.mark.parametrize(
    "brand",
    sorted(b for b in uv._HEIC_BRANDS),
    ids=lambda b: b.decode(),
)
def test_looks_like_image_accepts_all_heic_brands(brand):
    head = b"\x00\x00\x00\x18ftyp" + brand + b"\x00\x00\x00\x00"
    assert uv._looks_like_image(head) is True


# ----------------------------------------------------------------------------
# _looks_like_image — rejeicoes (lixo e bordas)
# ----------------------------------------------------------------------------
@pytest.mark.parametrize(
    "head",
    [
        b"",  # vazio
        b"hello world not an image",  # texto arbitrario
        b"%PDF-1.4",  # PDF
        b"MZ\x90\x00 executavel",  # PE/EXE
        b"\x00\x00\x00\x00\x00\x00\x00\x00",  # zeros
        b"RIFF\x24\x00\x00\x00AVI ",  # RIFF mas nao WEBP (AVI)
        b"\x00\x00\x00\x18ftypmp42",  # ftyp mas marca nao-HEIC (MP4)
        b"\xff\xd8",  # JPEG truncado (so 2 dos 3 bytes da assinatura)
    ],
    ids=["empty", "text", "pdf", "exe", "zeros", "riff_avi", "ftyp_mp4", "jpeg_trunc"],
)
def test_looks_like_image_rejects_non_images(head):
    assert uv._looks_like_image(head) is False


def test_looks_like_image_webp_requires_riff_and_webp_marker():
    # RIFF presente mas marcador WEBP ausente -> falso.
    assert uv._looks_like_image(b"RIFF\x00\x00\x00\x00XXXX") is False


def test_looks_like_image_heic_requires_min_length():
    # ftyp + marca valida mas head curto demais (< 12) -> rejeitado pela guarda len>=12.
    short = b"\x00\x00\x00\x18ftypheic"[:11]
    assert len(short) < 12
    assert uv._looks_like_image(short) is False


# ----------------------------------------------------------------------------
# read_image_upload_safely — caminhos felizes
# ----------------------------------------------------------------------------
def test_read_image_returns_full_bytes_for_valid_jpeg():
    payload = JPEG + b"\x00" * 5000
    result = _run(uv.read_image_upload_safely(_FakeUploadFile(payload)))
    assert result == payload


def test_read_image_reassembles_multiple_chunks():
    # Maior que _CHUNK_SIZE (64KB) para forcar varias leituras.
    payload = PNG + b"\x42" * (uv._CHUNK_SIZE * 2 + 123)
    result = _run(uv.read_image_upload_safely(_FakeUploadFile(payload)))
    assert result == payload
    assert len(result) == len(payload)


def test_read_image_head_assembled_across_chunk_boundary(monkeypatch):
    # Se a assinatura ficar dividida entre chunks pequenos, o head ainda deve
    # ser montado corretamente (head acumula ate _HEAD_SIZE bytes).
    monkeypatch.setattr(uv, "_CHUNK_SIZE", 2)
    payload = PNG + b"\x00" * 10
    result = _run(uv.read_image_upload_safely(_FakeUploadFile(payload)))
    assert result == payload


# ----------------------------------------------------------------------------
# read_image_upload_safely — bordas e erros
# ----------------------------------------------------------------------------
def test_read_image_empty_file_raises_400():
    with pytest.raises(HTTPException) as exc:
        _run(uv.read_image_upload_safely(_FakeUploadFile(b"")))
    assert exc.value.status_code == 400
    assert "vazio" in exc.value.detail.lower()


def test_read_image_non_image_raises_400():
    payload = b"this is plainly not an image at all, just text bytes here ok"
    with pytest.raises(HTTPException) as exc:
        _run(uv.read_image_upload_safely(_FakeUploadFile(payload)))
    assert exc.value.status_code == 400
    assert "imagem" in exc.value.detail.lower()


def test_read_image_over_limit_raises_413():
    payload = JPEG + b"\x00" * 100
    with pytest.raises(HTTPException) as exc:
        _run(uv.read_image_upload_safely(_FakeUploadFile(payload), max_bytes=50))
    assert exc.value.status_code == 413
    assert "limite" in exc.value.detail.lower()


def test_read_image_exactly_at_limit_is_allowed():
    # total > max_bytes dispara 413; total == max_bytes passa (estritamente maior).
    payload = JPEG + b"\x00" * 6  # len(JPEG)=10 -> total 16
    assert len(payload) == 16
    result = _run(uv.read_image_upload_safely(_FakeUploadFile(payload), max_bytes=16))
    assert result == payload


def test_read_image_one_over_limit_raises_413():
    payload = JPEG + b"\x00" * 7  # total 17 > 16
    assert len(payload) == 17
    with pytest.raises(HTTPException) as exc:
        _run(uv.read_image_upload_safely(_FakeUploadFile(payload), max_bytes=16))
    assert exc.value.status_code == 413


def test_read_image_limit_message_uses_megabytes():
    payload = JPEG + b"\x00" * (3 * 1024 * 1024)
    with pytest.raises(HTTPException) as exc:
        _run(uv.read_image_upload_safely(_FakeUploadFile(payload), max_bytes=2 * 1024 * 1024))
    assert "2 MB" in exc.value.detail


def test_read_image_size_check_runs_before_image_validation():
    # Conteudo NAO-imagem mas grande: o 413 (tamanho) deve disparar antes do 400 (magic bytes),
    # pois a checagem de tamanho ocorre dentro do loop de leitura.
    payload = b"X" * 100  # nao e imagem
    with pytest.raises(HTTPException) as exc:
        _run(uv.read_image_upload_safely(_FakeUploadFile(payload), max_bytes=50))
    assert exc.value.status_code == 413


def test_default_max_upload_bytes_is_15mb():
    assert uv.MAX_UPLOAD_BYTES == 15 * 1024 * 1024


# ----------------------------------------------------------------------------
# enforce_upload_rate_limit
# ----------------------------------------------------------------------------
@pytest.fixture
def fresh_limiter(monkeypatch):
    """Substitui o limiter de modulo por um isolado e controlavel."""
    limiter = InMemoryLoginRateLimiter(max_failures=3, window_seconds=600.0)
    monkeypatch.setattr(uv, "upload_rate_limiter", limiter)
    return limiter


def test_enforce_records_failure_per_request(fresh_limiter):
    req = _FakeRequest("10.0.0.1")
    uv.enforce_upload_rate_limit(req)
    uv.enforce_upload_rate_limit(req)
    # 2 registros, abaixo de max_failures=3 -> ainda nao bloqueado.
    assert len(fresh_limiter._recent_failures("10.0.0.1")) == 2


def test_enforce_blocks_after_threshold(fresh_limiter):
    req = _FakeRequest("10.0.0.2")
    # max_failures=3: as 3 primeiras passam (is_blocked checa ANTES de registrar).
    uv.enforce_upload_rate_limit(req)
    uv.enforce_upload_rate_limit(req)
    uv.enforce_upload_rate_limit(req)
    # 4a chamada: ja ha 3 registros >= max_failures -> bloqueio 429.
    with pytest.raises(HTTPException) as exc:
        uv.enforce_upload_rate_limit(req)
    assert exc.value.status_code == 429
    assert "envios" in exc.value.detail.lower()


def test_enforce_isolates_by_ip(fresh_limiter):
    a = _FakeRequest("10.0.0.10")
    b = _FakeRequest("10.0.0.11")
    for _ in range(3):
        uv.enforce_upload_rate_limit(a)
    # IP 'a' agora bloqueia; IP 'b' permanece livre.
    with pytest.raises(HTTPException):
        uv.enforce_upload_rate_limit(a)
    uv.enforce_upload_rate_limit(b)  # nao deve levantar
    assert len(fresh_limiter._recent_failures("10.0.0.11")) == 1


def test_enforce_uses_unknown_when_no_client(fresh_limiter):
    req = _FakeRequest(host=None)  # request.client is None
    uv.enforce_upload_rate_limit(req)
    assert len(fresh_limiter._recent_failures("unknown")) == 1


def test_enforce_uses_unknown_when_empty_host(fresh_limiter):
    req = _FakeRequest(host="")  # host falsy -> fallback "unknown"
    uv.enforce_upload_rate_limit(req)
    assert len(fresh_limiter._recent_failures("unknown")) == 1


def test_enforce_handles_none_request(fresh_limiter):
    # request=None: o guard `request and request.client` cobre isso -> "unknown".
    uv.enforce_upload_rate_limit(None)
    assert len(fresh_limiter._recent_failures("unknown")) == 1


def test_enforce_window_expiry_with_injected_clock(monkeypatch):
    # Usa clock injetado (sem sleep real) para validar expiracao da janela.
    now = {"t": 1000.0}
    limiter = InMemoryLoginRateLimiter(
        max_failures=2, window_seconds=100.0, clock=lambda: now["t"]
    )
    monkeypatch.setattr(uv, "upload_rate_limiter", limiter)
    req = _FakeRequest("10.0.0.20")
    uv.enforce_upload_rate_limit(req)
    uv.enforce_upload_rate_limit(req)
    # 2 registros >= max_failures=2 -> bloqueado agora.
    with pytest.raises(HTTPException):
        uv.enforce_upload_rate_limit(req)
    # Avanca o tempo alem da janela: registros antigos expiram -> liberado.
    now["t"] += 101.0
    uv.enforce_upload_rate_limit(req)  # nao deve levantar
    assert len(limiter._recent_failures("10.0.0.20", now["t"])) == 1
