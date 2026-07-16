"""
Trava de segurança da suíte de testes (Onda 0 — test-T1 / mt-MT7 / sec-SEC7).

Garante que NENHUM teste toque um banco de dados remoto / de produção.

Este módulo é importado pelo pytest ANTES de qualquer import de
``app.core.database``, então ele força o uso de um SQLite local. Como
``app.core.database`` chama ``load_dotenv(override=False)``, estas variáveis
de ambiente NÃO serão sobrescritas pelo ``backend/.env`` (que aponta para
produção). Defesa em profundidade: ``pytest_configure`` reconfere a URL que o
app realmente resolveu e ABORTA a sessão inteira se detectar um host remoto.
"""
import os
from urllib.parse import urlparse

# 1) Forçar banco LOCAL e isolado antes do app importar a camada de banco.
_TEST_DB_URL = "sqlite:///./test_aumigao.db"
os.environ["DATABASE_URL"] = _TEST_DB_URL
os.environ["URL_DO_BANCO_DE_DADOS"] = _TEST_DB_URL

# 2) Nunca rodar seed de admin nem DDL destrutivo durante os testes.
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("RUN_STARTUP_ADMIN_SEED", "false")
os.environ.setdefault("RUN_LEGACY_ID_COMPAT", "false")

# 2b) Gate de pagamento (R7): o DEFAULT DE PRODUÇÃO é LIGADO (fail-closed), mas os
#     testes LEGADOS de criação/matching foram escritos assumindo o passeio já
#     nascendo agendado. Para não reescrever dezenas de suítes, a suíte roda com o
#     gate DESLIGADO por padrão; os testes NOVOS do gate ligam-no explicitamente via
#     monkeypatch.setenv("REQUIRE_PAYMENT_BEFORE_MATCHING", "true"), que sobrepõe isto.
os.environ.setdefault("REQUIRE_PAYMENT_BEFORE_MATCHING", "false")

# 2c) Aceite legal BLOQUEANTE (feat/aceite-legal-bloqueante): o DEFAULT DE PRODUCAO
#     e LIGADO (fail-closed), mas as suites LEGADAS de rota exercitam
#     create_walk/create_payment/accept/subscribe sem passar pelo fluxo de aceite.
#     Para nao reescrever ~17 suites, a suite roda com o enforcement DESLIGADO por
#     padrao; os testes NOVOS do aceite ligam via monkeypatch.setenv(
#     "LEGAL_ACCEPTANCE_ENFORCED", "true"). Mesmo padrao do gate de pagamento acima.
os.environ.setdefault("LEGAL_ACCEPTANCE_ENFORCED", "false")

# 2d) Cache de dados (data_cache): o DEFAULT DE PRODUCAO e LIGADO, mas as suites
#     legadas escrevem direto no banco e releem o app-config esperando frescor
#     imediato — com cache ligado receberiam resposta velha (TTL 60s). A suite
#     roda com o cache DESLIGADO por padrao; os testes do cache ligam via
#     monkeypatch.setenv("DATA_CACHE_ENABLED", "true"). Mesmo padrao do 2b/2c.
os.environ.setdefault("DATA_CACHE_ENABLED", "false")

# 3) Chave fixa de cifragem de PII (CPF/RG) para os testes — Fernet key válida de 32 bytes.
#    NUNCA usar em produção (lá vem de PII_ENCRYPTION_KEY no ambiente).
#    Gerada com: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
os.environ.setdefault("PII_ENCRYPTION_KEY", "sI9VJYXwVrM29Mykh649L9MzxjbneiYu3dI9X6k29ws=")


def _is_local_sqlite(url: str) -> bool:
    if not url:
        return False
    parsed = urlparse(url)
    if parsed.scheme.startswith("sqlite"):
        return True
    host = (parsed.hostname or "").lower()
    return host in {"localhost", "127.0.0.1", "::1"}


import pytest


@pytest.fixture(autouse=True)
def _reset_pii_crypto_cache():
    """Limpa o cache do _fernet() entre testes para evitar contaminação de chaves."""
    try:
        from app.core.pii_crypto import _fernet
        _fernet.cache_clear()
    except ImportError:
        pass
    yield
    try:
        from app.core.pii_crypto import _fernet
        _fernet.cache_clear()
    except ImportError:
        pass


@pytest.fixture(autouse=True)
def _reset_ip_rate_limiters():
    """Zera os rate limiters por IP entre testes (singletons de módulo — A4 hardening)."""
    try:
        from app.routes.auth import _login_ip_rate_limiter, _register_rate_limiter, _social_rate_limiter
        _register_rate_limiter._failures.clear()
        _social_rate_limiter._failures.clear()
        _login_ip_rate_limiter._failures.clear()
        yield
        _register_rate_limiter._failures.clear()
        _social_rate_limiter._failures.clear()
        _login_ip_rate_limiter._failures.clear()
    except ImportError:
        yield
    try:
        from app.services.upload_validation import application_rate_limiter, upload_rate_limiter
        application_rate_limiter._failures.clear()
        upload_rate_limiter._failures.clear()
    except ImportError:
        pass


@pytest.fixture(autouse=True)
def _reset_data_cache():
    """Zera o fallback in-memory do data_cache entre testes (singleton de módulo)."""
    try:
        from app.core.data_cache import data_cache
        data_cache._fallback.clear()
        yield
        data_cache._fallback.clear()
    except ImportError:
        yield


def pytest_configure(config):
    """Backstop: confirma que o app resolveu um banco local antes de rodar nada."""
    try:
        from app.core.database import SQLALCHEMY_DATABASE_URL as resolved
    except Exception:
        # Se o app sequer importa, deixe o erro real aparecer nos próprios testes.
        return
    if not _is_local_sqlite(resolved):
        safe = resolved.split("@")[-1]  # esconde usuário:senha, mostra só o host
        raise SystemExit(
            "\n\n[ABORT-SEGURANCA] A suite de testes tentou usar um banco NAO-local:\n"
            f"  -> {safe}\n"
            "Os testes so podem rodar contra SQLite local.\n"
            "Verifique DATABASE_URL no ambiente e o arquivo backend/.env.\n"
        )
