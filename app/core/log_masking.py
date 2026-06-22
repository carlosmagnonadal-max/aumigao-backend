"""
PII masking for logging — LGPD compliance.

Provides:
  - sanitize_for_log(value): recursive dict/list sanitizer (DRY canonical version).
    Replaces the local copy in app/routes/payments.py — that module imports from here.
  - mask_email(email): masks email address keeping first char + domain.
  - SensitiveDataFilter: logging.Filter that redacts PII from any LogRecord's
    msg, args and extra fields before the record reaches any handler.

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
_EMAIL_RE = re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+")


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
    """Mask CPF and e-mail patterns found in free-form log strings."""
    text = _CPF_RE.sub("***", text)
    text = _EMAIL_RE.sub(lambda m: mask_email(m.group(0)), text)
    return text


def _redact(value: Any) -> Any:
    """Recursively redact sensitive data from an arbitrary value."""
    if isinstance(value, dict):
        return sanitize_for_log(value)
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        return _mask_string(value)
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
