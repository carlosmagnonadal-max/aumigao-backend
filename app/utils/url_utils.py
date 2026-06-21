"""Utilitários de normalização de URLs de mídia.

Centraliza a lógica de sanitização de URLs de foto/documento enviadas
pelo app mobile (que pode gerar URLs locais como file:, content:, blob:).
"""

# URL Railway legada (HTTP) — migrado para Cloud Run, mas dados antigos podem
# ainda carregar este hostname.
_RAILWAY_HTTP = "http://aumigao-backend-production.up.railway.app"
_RAILWAY_HTTPS = "https://aumigao-backend-production.up.railway.app"

# Prefixos que indicam URL local do dispositivo (inútil no servidor)
_LOCAL_PREFIXES = ("file:", "content:", "blob:", "data:image")


def normalize_media_url(value: str | None) -> str | None:
    """Retorna a URL normalizada ou None se for URL local/inválida.

    - Descarta URLs de dispositivo (file:, content:, blob:, data:image).
    - Converte Railway http→https.
    - URLs já válidas são retornadas sem alteração.
    """
    url = (value or "").strip()
    if not url:
        return None
    if url.startswith(_LOCAL_PREFIXES):
        return None
    if url.startswith(_RAILWAY_HTTP):
        return url.replace(_RAILWAY_HTTP, _RAILWAY_HTTPS, 1)
    return url
