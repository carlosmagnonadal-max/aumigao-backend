"""
PII encryption helpers — CPF/RG at rest (Fernet AES-128-CBC + HMAC).

Design constraints:
- Passthrough de vazio: encrypt("") == "" e decrypt("") == ""
- decrypt é TOLERANTE a texto puro legado (InvalidToken → retorna o valor original)
- blind_index é determinístico e normaliza CPF antes do HMAC para que "123.456.789-00"
  e "12345678900" gerem o mesmo índice
- _fernet() levanta RuntimeError só no uso, nunca no import
- Nenhum valor de CPF/RG é logado neste módulo
"""

import hashlib
import hmac
import os
import re
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import String
from sqlalchemy.types import TypeDecorator


# ---------------------------------------------------------------------------
# Chave / instância Fernet
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    """Retorna a instância Fernet cacheada.

    Lê PII_ENCRYPTION_KEY do ambiente.  Falha com RuntimeError claro se ausente
    ou vazio — nunca falha no import, só no primeiro uso real.
    """
    key = os.getenv("PII_ENCRYPTION_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "PII_ENCRYPTION_KEY não está definida no ambiente. "
            "Defina a variável com uma chave Fernet de 32 bytes base64-url-encoded "
            "antes de operar com dados de CPF/RG cifrados."
        )
    try:
        return Fernet(key.encode())
    except Exception as exc:
        raise RuntimeError(
            f"PII_ENCRYPTION_KEY é inválida: {exc}. "
            "Gere uma chave válida com: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        ) from exc


# ---------------------------------------------------------------------------
# Normalização
# ---------------------------------------------------------------------------

def normalize_cpf(v: str) -> str:
    """Remove qualquer caractere não-dígito do CPF."""
    return re.sub(r"\D", "", v or "")


# ---------------------------------------------------------------------------
# Encrypt / Decrypt
# ---------------------------------------------------------------------------

def encrypt(plaintext: str) -> str:
    """Cifra plaintext com Fernet.

    - None ou "" → passthrough (retorna o valor sem modificar).
    - Qualquer outra string → token Fernet base64-url.
    """
    if not plaintext:
        return plaintext  # type: ignore[return-value]
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    """Decifra um token Fernet.

    - None ou "" → passthrough.
    - Token válido → plaintext.
    - Token inválido (texto puro legado, chave errada) → retorna o token original
      sem modificar.  Tolerância intencional para migração sem regressão.
    """
    if not token:
        return token  # type: ignore[return-value]
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        return token


# ---------------------------------------------------------------------------
# Detecção de valor já cifrado (idempotência da migration)
# ---------------------------------------------------------------------------

def is_encrypted(v: str) -> bool:
    """Heurística: retorna True se v é um token Fernet válido para a chave atual."""
    if not v:
        return False
    try:
        _fernet().decrypt(v.encode())
        return True
    except InvalidToken:
        return False


# ---------------------------------------------------------------------------
# Blind index (HMAC-SHA256 determinístico, normalizado)
# ---------------------------------------------------------------------------

def blind_index(v: str | None) -> str | None:
    """Retorna HMAC-SHA256 do CPF normalizado usando PII_ENCRYPTION_KEY como chave.

    - None ou "" → None (sem índice para valor vazio).
    - "123.456.789-00" e "12345678900" produzem o MESMO índice.

    A chave do HMAC é os bytes UTF-8 do PII_ENCRYPTION_KEY (string), não os
    bytes da chave Fernet decodificada — isso mantém independência e evita
    re-derivação complexa enquanto ainda usa segredo seguro.
    """
    if not v:
        return None
    digits = normalize_cpf(v)
    if not digits:
        return None
    key_bytes = os.getenv("PII_ENCRYPTION_KEY", "").strip().encode()
    if not key_bytes:
        raise RuntimeError(
            "PII_ENCRYPTION_KEY não está definida — blind_index não pode ser calculado."
        )
    return hmac.new(key_bytes, digits.encode(), hashlib.sha256).hexdigest()


# ---------------------------------------------------------------------------
# SQLAlchemy TypeDecorator
# ---------------------------------------------------------------------------

class EncryptedString(TypeDecorator):
    """Coluna String que armazena valores cifrados com Fernet transparentemente.

    - Leitura: decrypt (tolerante a legado)
    - Escrita: encrypt (passthrough de vazio)
    """

    impl = String
    cache_ok = True

    def process_bind_param(self, value, dialect):
        """Chamado ao persistir: cifra o valor antes de gravar no banco."""
        if not value:
            return value
        return encrypt(value)

    def process_result_value(self, value, dialect):
        """Chamado ao ler: decifra o valor retornado pelo banco."""
        if not value:
            return value
        return decrypt(value)
