import hmac
import os
import time
from hashlib import sha256
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit


# Raiz dos uploads. Configurável por env UPLOADS_DIR para apontar a um volume
# persistente (ex.: Railway), evitando perda de arquivos no filesystem efêmero.
# Default = ./uploads na raiz do backend (comportamento anterior; dev/local).
UPLOAD_ROOT = Path(os.getenv("UPLOADS_DIR") or (Path(__file__).resolve().parents[2] / "uploads"))
SIGNED_UPLOAD_TTL_SECONDS = int(os.getenv("SIGNED_UPLOAD_TTL_SECONDS", "600"))
SENSITIVE_WALKER_DOCUMENT_PREFIXES = (
    "identity_front-",
    "identity_back-",
    "address_proof-",
    "selfie-",
)


def _is_production_environment() -> bool:
    environment = (os.getenv("ENVIRONMENT") or os.getenv("RAILWAY_ENVIRONMENT") or "").strip().lower()
    return environment in {"production", "prod"}


def _signing_secret() -> str:
    secret = (os.getenv("SIGNED_UPLOAD_SECRET") or os.getenv("JWT_SECRET") or "").strip().strip('"').strip("'")
    if secret:
        return secret
    if _is_production_environment():
        raise RuntimeError("SIGNED_UPLOAD_SECRET ou JWT_SECRET deve estar configurado para URLs assinadas.")
    return "aumigao-dev-test-signed-uploads-secret-min-32"


def normalize_upload_path(upload_path: str) -> str | None:
    normalized = (upload_path or "").replace("\\", "/").lstrip("/")
    if normalized.startswith("uploads/"):
        normalized = normalized[len("uploads/") :]
    parts = [part for part in normalized.split("/") if part]
    if not parts or any(part in {".", ".."} for part in parts):
        return None
    candidate = (UPLOAD_ROOT / "/".join(parts)).resolve()
    try:
        candidate.relative_to(UPLOAD_ROOT.resolve())
    except ValueError:
        return None
    return "/".join(parts)


def upload_file_path(upload_path: str) -> Path | None:
    normalized = normalize_upload_path(upload_path)
    if not normalized:
        return None
    return (UPLOAD_ROOT / normalized).resolve()


def is_sensitive_upload_path(upload_path: str) -> bool:
    normalized = normalize_upload_path(upload_path)
    if not normalized:
        return False
    parts = normalized.split("/")
    if len(parts) < 3 or parts[0] != "walker-documents":
        return False
    filename = parts[-1]
    return filename.startswith(SENSITIVE_WALKER_DOCUMENT_PREFIXES)


def _signature(upload_path: str, expires: int) -> str:
    message = f"{upload_path}:{expires}".encode("utf-8")
    return hmac.new(_signing_secret().encode("utf-8"), message, sha256).hexdigest()


def create_signed_upload_url(upload_url: str | None, ttl_seconds: int = SIGNED_UPLOAD_TTL_SECONDS) -> str | None:
    if not upload_url:
        return upload_url
    split = urlsplit(upload_url)
    path = split.path or upload_url
    marker = "/uploads/"
    if marker not in path:
        return upload_url
    upload_path = path.split(marker, 1)[1]
    normalized = normalize_upload_path(upload_path)
    if not normalized or not is_sensitive_upload_path(normalized):
        return upload_url
    expires = int(time.time()) + ttl_seconds
    query = urlencode({"expires": str(expires), "signature": _signature(normalized, expires)})
    return urlunsplit((split.scheme, split.netloc, split.path, query, split.fragment))


def has_valid_upload_signature(upload_path: str, query_string: str | bytes) -> bool:
    normalized = normalize_upload_path(upload_path)
    if not normalized:
        return False
    raw_query = query_string.decode("utf-8") if isinstance(query_string, bytes) else query_string
    params = parse_qs(raw_query, keep_blank_values=True)
    expires_values = params.get("expires") or []
    signature_values = params.get("signature") or []
    if not expires_values or not signature_values:
        return False
    try:
        expires = int(expires_values[0])
    except (TypeError, ValueError):
        return False
    if expires < int(time.time()):
        return False
    expected = _signature(normalized, expires)
    return hmac.compare_digest(expected, signature_values[0])
