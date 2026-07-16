"""Testes de app/lib/branding_image.py (normalize_logo_image) — funcao PURA.

Sem FastAPI, sem banco, sem storage: gera imagens em memoria com Pillow e
valida o comportamento de trim de margem + resize + saida em PNG.
"""
import io

import pytest
from PIL import Image

from app.lib.branding_image import MAX_DIMENSION, normalize_logo_image


def _png_bytes(img: Image.Image) -> bytes:
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    return buffer.getvalue()


def _jpeg_bytes(img: Image.Image) -> bytes:
    buffer = io.BytesIO()
    img.save(buffer, format="JPEG", quality=95)
    return buffer.getvalue()


def _opened(content: bytes) -> Image.Image:
    return Image.open(io.BytesIO(content))


# --------------------------------------------------------------- (a) trim alpha --


def test_trim_removes_transparent_margin_and_centers_content():
    """PNG com margem transparente grande -> trim reduz dimensoes e o
    desenho fica aproximadamente centralizado no resultado."""
    size = (200, 200)
    img = Image.new("RGBA", size, (0, 0, 0, 0))  # tudo transparente
    # desenho opaco ocupando uma faixa central bem menor que o canvas.
    for x in range(60, 140):
        for y in range(80, 120):
            img.putpixel((x, y), (200, 30, 30, 255))

    out_bytes, out_content_type = normalize_logo_image(_png_bytes(img), "image/png")
    assert out_content_type == "image/png"

    out = _opened(out_bytes)
    assert out.size[0] < size[0]
    assert out.size[1] < size[1]

    # bbox do conteudo opaco no resultado deve estar perto do centro da nova imagem.
    alpha = out.convert("RGBA").getchannel("A")
    bbox = alpha.point(lambda a: 255 if a > 10 else 0).getbbox()
    assert bbox is not None
    content_center_x = (bbox[0] + bbox[2]) / 2
    content_center_y = (bbox[1] + bbox[3]) / 2
    out_w, out_h = out.size
    # tolerancia generosa (margem de seguranca nao e simetrica por causa do
    # arredondamento, mas o desenho deve ficar perto do meio, nao encostado
    # numa borda).
    assert abs(content_center_x - out_w / 2) < out_w * 0.25
    assert abs(content_center_y - out_h / 2) < out_h * 0.25


# ------------------------------------------------------------- (b) trim cor ------


def test_trim_removes_white_border_from_opaque_jpeg():
    """JPG opaco com borda branca grossa -> trim reduz as dimensoes."""
    size = (200, 200)
    img = Image.new("RGB", size, (255, 255, 255))
    for x in range(70, 130):
        for y in range(70, 130):
            img.putpixel((x, y), (20, 60, 120))

    out_bytes, out_content_type = normalize_logo_image(_jpeg_bytes(img), "image/jpeg")
    assert out_content_type == "image/png"  # saida sempre PNG

    out = _opened(out_bytes)
    assert out.size[0] < size[0]
    assert out.size[1] < size[1]


# ------------------------------------------------------------ (c) resize down ---


def test_resize_down_when_larger_than_max_dimension():
    """Imagem maior que MAX_DIMENSION no lado maior -> reduzida, nunca aumentada."""
    size = (2000, 1000)
    img = Image.new("RGBA", size, (10, 20, 30, 255))

    out_bytes, _ = normalize_logo_image(_png_bytes(img), "image/png")
    out = _opened(out_bytes)

    assert max(out.size) <= MAX_DIMENSION
    # mantem proporcao 2:1
    assert abs((out.size[0] / out.size[1]) - 2.0) < 0.02


def test_does_not_upscale_small_image():
    """Imagem pequena permanece pequena (a funcao nunca aumenta)."""
    size = (40, 40)
    img = Image.new("RGBA", size, (10, 20, 30, 255))

    out_bytes, _ = normalize_logo_image(_png_bytes(img), "image/png")
    out = _opened(out_bytes)
    assert max(out.size) <= max(size)


# --------------------------------------------------------- (d) imagem ja justa --


def test_tight_image_stays_practically_unchanged():
    """Imagem sem margem (cor uniforme preenchendo 100% do canvas, sem
    diferenca detectavel contra o canto) nao sofre trim — o bbox e
    degenerado (imagem inteira e "fundo") e a funcao preserva o tamanho."""
    size = (120, 80)
    img = Image.new("RGBA", size, (5, 5, 5, 255))  # cor solida, sem margem alguma

    out_bytes, _ = normalize_logo_image(_png_bytes(img), "image/png")
    out = _opened(out_bytes)
    assert out.size == size


# --------------------------------------------------- (e) imagem toda branca/transp --


def test_fully_white_image_does_not_explode():
    """Imagem 100% branca (opaca): nao ha 'desenho' — nao deve lancar excecao
    nem produzir recorte vazio; devolve algo valido (imagem preservada)."""
    size = (100, 100)
    img = Image.new("RGBA", size, (255, 255, 255, 255))

    out_bytes, out_content_type = normalize_logo_image(_png_bytes(img), "image/png")
    assert out_content_type == "image/png"
    out = _opened(out_bytes)
    assert out.size[0] > 0 and out.size[1] > 0


def test_fully_transparent_image_does_not_explode():
    """Imagem 100% transparente: idem, sem excecao e sem recorte vazio."""
    size = (100, 100)
    img = Image.new("RGBA", size, (0, 0, 0, 0))

    out_bytes, out_content_type = normalize_logo_image(_png_bytes(img), "image/png")
    assert out_content_type == "image/png"
    out = _opened(out_bytes)
    assert out.size[0] > 0 and out.size[1] > 0


# ------------------------------------------------------------- (f) bytes invalidos --


def test_invalid_bytes_raise_value_error():
    with pytest.raises(ValueError):
        normalize_logo_image(b"isto nao e uma imagem, so texto qualquer", "image/png")


def test_truncated_png_raises_value_error():
    """Cabecalho PNG valido mas dados incompletos/corrompidos -> ValueError
    (Image.open aceita o magic byte, mas .load() força a decodificacao e
    falha em dados invalidos)."""
    with pytest.raises(ValueError):
        normalize_logo_image(b"\x89PNG\r\n\x1a\n" + b"\x00" * 50, "image/png")
