"""
PII masking for logging — LGPD compliance.

Provides:
  - sanitize_for_log(value): recursive dict/list sanitizer (DRY canonical version).
    Replaces the local copy in app/routes/payments.py — that module imports from here.
  - mask_email(email): masks email address keeping first char + domain.
  - SensitiveDataFilter: logging.Filter that redacts PII from any LogRecord's
    msg, args, extra fields AND tracebacks (exc_info/exc_text, stack_info, chained
    exceptions) before the record reaches any handler.
  - MaskingFormatterMixin: masks formatException()/formatStack() output at the
    formatter level (covers formatters that re-render exc_info ignoring exc_text,
    e.g. python-json-logger).

Registration: configure_logging() in app/core/logging_config.py adds this filter to
the root logger so ALL loggers in the app are covered automatically.
"""
from __future__ import annotations

import logging
import re
from typing import Any

# Canonical set of sensitive keys (union of payments.py + audit_service.py sets).
SENSITIVE_KEYS: frozenset[str] = frozenset({
    "password",
    "password_hash",
    "token",
    "access_token",
    "refresh_token",
    "authorization",
    "api_key",
    "secret",
    "cpf",
    "cpf_cnpj",
    "cpfcnpj",
    "rg",
    "email",
})

# Regex to detect CPF patterns in free-form text (11 digits, optionally formatted).
_CPF_RE = re.compile(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b")
# Regex to detect bare e-mail addresses in free-form strings.
# Quantifiers are bounded (RFC local-part <= 64) so long tracebacks/blobs can't
# trigger quadratic backtracking; _mask_string also skips it when there is no "@".
_EMAIL_RE = re.compile(r"[a-zA-Z0-9_.+-]{1,64}@[a-zA-Z0-9-]{1,253}\.[a-zA-Z0-9-.]{1,253}")

# --- Secrets in free-form text (sec-audit 2026-08-04: a Resend key leaked via a
# traceback — httpx LocalProtocolError "Illegal header value b'Bearer re_...\r\n'").
# Values starting with "%" are never touched so %-style format strings in
# record.msg ("token=%s") keep matching their args.
# Authorization schemes: "Bearer <token>" (any case) / "Basic <b64>" (case-sensitive,
# so prose like "basic configuration" is left alone).
_AUTH_SCHEME_RE = re.compile(r"\b((?i:bearer)|Basic)(\s+)[A-Za-z0-9._~+/=\-]{8,}")
# Provider API keys with well-known prefixes (Resend re_, Stripe sk_/pk_/rk_/whsec_).
# Require at least one digit so Python identifiers like "re_enable_something" in
# tracebacks are not mangled.
_PREFIXED_KEY_RE = re.compile(
    r"\b(?:re|sk|pk|rk|whsec)_(?=[A-Za-z0-9_]{0,256}\d)[A-Za-z0-9_]{16,}"
)
# Asaas API keys ("$aact_prod_...", may contain ':' and '$').
_ASAAS_KEY_RE = re.compile(r"\$aact_[^\s'\"]+")
# JSON Web Tokens.
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*")
# Credentials embedded in URLs: scheme://user:password@host (anchored on the literal
# "://" with bounded parts — linear time on long strings).
_URL_CREDS_RE = re.compile(r"(://[^\s:/@]{1,256}:)(?!%)[^\s@/]{1,256}@")
# key=value / key: value / 'key': 'value' for sensitive key names. The key is matched
# as a suffix ("api_token=", "x-api-key:", "client_secret=") without a leading \b or
# prefix scan, which keeps it linear; "max_tokens=5"/"token_version=3" don't match.
_SENSITIVE_KV_RE = re.compile(
    r"(?i)(password|passwd|senha|secret|api[_\-]?key|token|authorization|cpf_?cnpj)"
    r"([\"']?\s*[:=]\s*[\"']?)"
    r"(?![%*])([^\s\"',;&}\]\)]+)"
)
# Substrings (lower-case) that must be present for _SENSITIVE_KV_RE to possibly match.
_KV_HINTS = ("pass", "senha", "secret", "key", "token", "authorization", "cpf")


def sanitize_for_log(value: Any) -> Any:
    """Recursively redact sensitive keys from dicts/lists.

    This is the canonical, shared implementation. app/routes/payments.py imports
    this function directly — do NOT duplicate the logic there.
    """
    if isinstance(value, dict):
        sanitized: dict = {}
        for key, item in value.items():
            norm = key.lower()
            if norm in SENSITIVE_KEYS or "token" in norm or "key" in norm:
                sanitized[key] = "***"
            else:
                sanitized[key] = sanitize_for_log(item)
        return sanitized
    if isinstance(value, list):
        return [sanitize_for_log(item) for item in value]
    return value


def mask_email(email: str) -> str:
    """Return a masked version of an e-mail address.

    Example: "carlos@example.com" → "c***@example.com"
    """
    if not email or "@" not in email:
        return "***"
    local, domain = email.split("@", 1)
    return f"{local[:1]}***@{domain}"


def _mask_string(text: str) -> str:
    """Mask secrets (tokens, API keys, passwords, URL credentials), CPF and e-mail
    patterns found in free-form log strings. Idempotent (masking "***" is a no-op)."""
    # Cheap substring guards skip regexes that cannot match (hot path: every record).
    lower = text.lower()
    if "bearer" in lower or "Basic" in text:
        text = _AUTH_SCHEME_RE.sub(r"\1\2***", text)
    if "$aact_" in text:
        text = _ASAAS_KEY_RE.sub("***", text)
    if "_" in text:
        text = _PREFIXED_KEY_RE.sub("***", text)
    if "eyJ" in text:
        text = _JWT_RE.sub("***", text)
    if "://" in text:
        text = _URL_CREDS_RE.sub(r"\1***@", text)
    if any(hint in lower for hint in _KV_HINTS):
        text = _SENSITIVE_KV_RE.sub(r"\1\2***", text)
    text = _CPF_RE.sub("***", text)
    if "@" in text:
        text = _EMAIL_RE.sub(lambda m: mask_email(m.group(0)), text)
    return text


# Plain formatter used only to render tracebacks to text inside the filter.
_TRACEBACK_FORMATTER = logging.Formatter()


def mask_exception_text(record: logging.LogRecord) -> None:
    """Render + mask the record's traceback (exc_text) and stack_info in place.

    record.exc_info is deliberately NOT cleared: other consumers (e.g. the Sentry
    logging integration, which runs after the handlers) still need the live
    exception object. Standard formatters reuse the cached, masked exc_text; the
    JSON formatter re-renders exc_info, which MaskingFormatterMixin covers.
    formatException() goes through traceback.print_exception, so chained
    exceptions (__cause__/__context__) are part of the masked text.
    """
    if record.exc_info and not record.exc_text:
        try:
            record.exc_text = _TRACEBACK_FORMATTER.formatException(record.exc_info)
        except Exception:  # never break logging because of a weird exc_info
            record.exc_text = None
    if record.exc_text:
        record.exc_text = _mask_string(record.exc_text)
    if isinstance(record.stack_info, str) and record.stack_info:
        record.stack_info = _mask_string(record.stack_info)


class MaskingFormatterMixin:
    """Mixin for logging.Formatter subclasses: masks rendered tracebacks/stacks."""

    def formatException(self, ei) -> str:  # noqa: N802 (logging API name)
        return _mask_string(super().formatException(ei))  # type: ignore[misc]

    def formatStack(self, stack_info: str) -> str:  # noqa: N802 (logging API name)
        return _mask_string(super().formatStack(stack_info))  # type: ignore[misc]


def _redact(value: Any) -> Any:
    """Recursively redact sensitive data from an arbitrary value."""
    if isinstance(value, dict):
        return sanitize_for_log(value)
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        return _mask_string(value)
    if isinstance(value, BaseException):
        # logger.warning("falhou: %s", exc) — the exception text may carry secrets.
        # Only replace the object when masking actually changed something.
        try:
            text = str(value)
        except Exception:
            return value
        masked = _mask_string(text)
        return masked if masked != text else value
    return value


class SensitiveDataFilter(logging.Filter):
    """Logging filter that redacts PII from every LogRecord before emission.

    Operates on:
      - record.msg (the format string or plain message)
      - record.args (tuple/dict of format arguments)
      - Extra fields added via logger.xxx(..., extra={...})

    Control-flow is NEVER altered — the filter always returns True.
    """

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        # Redact the message string itself.
        if isinstance(record.msg, str):
            record.msg = _mask_string(record.msg)

        # Redact tracebacks (exc_info -> exc_text, stack_info, exception chains).
        if record.exc_info or record.exc_text or record.stack_info:
            mask_exception_text(record)

        # Redact format arguments.
        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: _redact(v) for k, v in record.args.items()}
            elif isinstance(record.args, tuple):
                record.args = tuple(_redact(a) for a in record.args)

        # Redact any extra fields injected directly onto the record.
        for attr in list(vars(record)):
            if attr in {
                "name", "msg", "args", "created", "filename", "funcName",
                "levelname", "levelno", "lineno", "module", "msecs",
                "pathname", "process", "processName", "relativeCreated",
                "stack_info", "thread", "threadName", "exc_info", "exc_text",
                # Our own injected fields — keep as-is (already safe or not PII).
                "request_id", "user_id", "tenant_id",
            }:
                continue
            val = getattr(record, attr, None)
            if isinstance(val, (dict, list, str)):
                setattr(record, attr, _redact(val))

        return True
