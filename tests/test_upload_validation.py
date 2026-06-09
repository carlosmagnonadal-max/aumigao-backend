import asyncio
from io import BytesIO

import pytest
from fastapi import HTTPException, UploadFile

from app.services.upload_validation import (
    InMemoryLoginRateLimiter,
    _looks_like_image,
    read_image_upload_safely,
)

JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01" + b"\x00" * 100
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100


def _upload(data: bytes) -> UploadFile:
    return UploadFile(file=BytesIO(data), filename="x.jpg")


def test_magic_bytes_accepts_real_images():
    assert _looks_like_image(JPEG)
    assert _looks_like_image(PNG)
    assert _looks_like_image(b"RIFF\x00\x00\x00\x00WEBP")


def test_magic_bytes_rejects_disguised_files():
    # PDF e executável com content_type "image/*" falsificado seriam aceitos pelo
    # header, mas os magic bytes os rejeitam.
    assert not _looks_like_image(b"%PDF-1.4 conteudo")
    assert not _looks_like_image(b"MZ\x90\x00 executavel")
    assert not _looks_like_image(b"<html>nao e imagem</html>")


def test_read_accepts_valid_jpeg():
    assert asyncio.run(read_image_upload_safely(_upload(JPEG))) == JPEG


def test_read_rejects_non_image():
    with pytest.raises(HTTPException) as exc:
        asyncio.run(read_image_upload_safely(_upload(b"%PDF-1.4 not an image")))
    assert exc.value.status_code == 400


def test_read_rejects_oversized():
    with pytest.raises(HTTPException) as exc:
        asyncio.run(read_image_upload_safely(_upload(JPEG + b"\x00" * 500), max_bytes=64))
    assert exc.value.status_code == 413


def test_read_rejects_empty():
    with pytest.raises(HTTPException) as exc:
        asyncio.run(read_image_upload_safely(_upload(b"")))
    assert exc.value.status_code == 400


def test_rate_limiter_blocks_after_limit():
    limiter = InMemoryLoginRateLimiter(max_failures=3, window_seconds=600)
    ip = "1.2.3.4"
    assert not limiter.is_blocked(ip)
    for _ in range(3):
        limiter.record_failure(ip)
    assert limiter.is_blocked(ip)
