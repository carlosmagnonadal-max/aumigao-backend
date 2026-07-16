"""branding_image.py — Normalização de imagens de logo do tenant (white label).

Problema (relato 2026-07-16): admins sobem logos com margens embutidas
(quadro transparente ou fundo branco ao redor do desenho) e o app renderiza
essa logo num container bem largo (~2.8:1 a 3.6:1) com `resizeMode: contain`.
Margem embutida => o desenho fica visualmente descentrado/pequeno dentro do
container, mesmo com contain "funcionando" corretamente do ponto de vista do
React Native (ele não sabe que há espaço morto dentro do próprio arquivo).
Além disso, uploads crus de celular podem vir enormes (vários MB, milhares
de pixels) para um espaço que nunca renderiza acima de ~1024px.

Esta função é PURA (bytes -> bytes) de propósito: fácil de testar sem tocar
storage/banco, e o endpoint de upload é só quem decide QUANDO chamar.
"""
from __future__ import annotations

import io

from PIL import Image, ImageChops

# Lado maior final, em pixels. O app nunca renderiza a logo maior que isso
# (containers de branding no app/admin são pequenos); reduzir no upload evita
# carregar payloads de câmera (4000px+) para um cartão de ~300px de largura.
MAX_DIMENSION = 1024

# Margem de segurança pós-trim, como fração do lado maior da imagem já
# cortada. Evita que o desenho encoste exatamente na borda do arquivo (o que
# pareceria "colado" em containers com contain).
_SAFETY_MARGIN_RATIO = 0.02

# Tolerância para o trim por transparência: alpha abaixo disso é considerado
# "fundo" (pixel efetivamente invisível), mesmo com pequeno ruído de
# compressão/antialiasing na borda do desenho.
_ALPHA_TRIM_THRESHOLD = 10

# Tolerância para o trim por cor de fundo (imagem opaca): diferença de canal
# (0-255) abaixo disso é considerada "mesma cor do canto" — cobre ruído leve
# de JPEG em fundos brancos/quase-brancos sem cortar sombras/desenho reais.
_COLOR_TRIM_THRESHOLD = 12

# Alpha mínimo (em qualquer pixel) para considerarmos que a imagem "usa"
# transparência de verdade. Abaixo disso (ex.: todo o alpha = 255, ou muito
# perto disso) tratamos como imagem opaca e usamos o trim por cor.
_OPAQUE_ALPHA_FLOOR = 250


class InvalidImageError(ValueError):
    """Bytes recebidos não são uma imagem válida/decodificável."""


def _open_image(content: bytes) -> Image.Image:
    try:
        img = Image.open(io.BytesIO(content))
        img.load()  # força a decodificação completa (pega arquivos truncados)
    except Exception as exc:  # noqa: BLE001 - Pillow levanta tipos variados
        raise InvalidImageError("Não foi possível abrir a imagem enviada.") from exc
    return img


def _alpha_bbox(rgba: Image.Image) -> tuple[int, int, int, int] | None:
    """Bounding box do conteúdo "visível" via canal alpha, com threshold."""
    alpha = rgba.getchannel("A")
    mask = alpha.point(lambda a: 255 if a > _ALPHA_TRIM_THRESHOLD else 0)
    return mask.getbbox()


def _color_bbox(rgba: Image.Image) -> tuple[int, int, int, int] | None:
    """Bounding box do conteúdo via diferença contra a cor do canto (0,0).

    Cobre o caso de imagem opaca (JPEG, ou PNG sem transparência real) com
    margem de fundo sólido (branco, quase-branco, ou qualquer cor uniforme).
    """
    rgb = rgba.convert("RGB")
    corner_color = rgb.getpixel((0, 0))
    background = Image.new("RGB", rgb.size, corner_color)
    diff = ImageChops.difference(rgb, background).convert("L")
    mask = diff.point(lambda p: 255 if p > _COLOR_TRIM_THRESHOLD else 0)
    return mask.getbbox()


def _is_degenerate_bbox(bbox: tuple[int, int, int, int] | None, size: tuple[int, int]) -> bool:
    """True se o bbox não existe ou é chapado a ponto de não valer o crop.

    Protege contra imagem 100% transparente/uniforme: nesse caso não há
    "desenho" para centralizar — melhor devolver a imagem original inteira do
    que gerar um recorte de poucos pixels (ou vazio).
    """
    if bbox is None:
        return True
    left, top, right, bottom = bbox
    width, height = right - left, bottom - top
    if width < 4 or height < 4:
        return True
    # bbox == imagem inteira não é degenerado (só significa "sem margem").
    return False


def _apply_safety_margin(
    bbox: tuple[int, int, int, int], size: tuple[int, int]
) -> tuple[int, int, int, int]:
    left, top, right, bottom = bbox
    width, height = size
    margin = max(1, round(_SAFETY_MARGIN_RATIO * max(right - left, bottom - top)))
    return (
        max(0, left - margin),
        max(0, top - margin),
        min(width, right + margin),
        min(height, bottom + margin),
    )


def _trim(rgba: Image.Image) -> Image.Image:
    """Recorta as margens (transparentes ou de cor uniforme) do desenho."""
    # Se o alpha varia de verdade (tem pixel abaixo do "floor" de opacidade),
    # o trim por transparência é o mais preciso — não depende de cor de fundo.
    alpha = rgba.getchannel("A")
    has_real_alpha = alpha.getextrema()[0] < _OPAQUE_ALPHA_FLOOR
    bbox = _alpha_bbox(rgba) if has_real_alpha else _color_bbox(rgba)

    if _is_degenerate_bbox(bbox, rgba.size):
        return rgba  # imagem em branco/uniforme: não há o que centralizar

    bbox = _apply_safety_margin(bbox, rgba.size)  # type: ignore[arg-type]
    return rgba.crop(bbox)


def _resize_down(img: Image.Image) -> Image.Image:
    """Reduz para no máximo MAX_DIMENSION no lado maior. Nunca aumenta."""
    width, height = img.size
    largest = max(width, height)
    if largest <= MAX_DIMENSION:
        return img
    scale = MAX_DIMENSION / largest
    new_size = (max(1, round(width * scale)), max(1, round(height * scale)))
    return img.resize(new_size, Image.LANCZOS)


def normalize_logo_image(content: bytes, content_type: str) -> tuple[bytes, str]:
    """Normaliza uma logo de tenant para uso em containers largos com `contain`.

    Passos: (1) abre e valida os bytes; (2) converte para RGBA; (3) recorta
    margens transparentes/uniformes com uma pequena margem de segurança para
    o desenho não encostar na borda; (4) reduz o lado maior para no máximo
    MAX_DIMENSION px (nunca aumenta); (5) sempre devolve PNG (preserva
    transparência, independente do formato de entrada).

    Levanta ValueError se os bytes não forem uma imagem decodificável.
    `content_type` hoje só é usado para contexto/logs futuros — a decisão de
    formato de saída é sempre PNG, então não precisamos ramificar por ele.
    """
    del content_type  # decisão de saída é sempre PNG; mantido na assinatura por clareza da API
    img = _open_image(content)
    rgba = img.convert("RGBA")
    trimmed = _trim(rgba)
    resized = _resize_down(trimmed)

    buffer = io.BytesIO()
    resized.save(buffer, format="PNG")
    return buffer.getvalue(), "image/png"
