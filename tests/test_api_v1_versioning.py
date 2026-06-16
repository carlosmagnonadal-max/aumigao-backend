"""api-T5 — /api/v1 versionado (aditivo). Os routers de NEGÓCIO são montados também
sob /api/v1, sem remover as rotas atuais (apps em uso não quebram). Console/admin
interno fica fora por ora."""
from app.main import app


def _paths() -> set[str]:
    return {getattr(r, "path", "") for r in app.routes}


def test_v1_business_routes_mounted():
    ps = _paths()
    assert "/api/v1/payments/quote" in ps
    assert any(p.startswith("/api/v1/auth") for p in ps)
    assert any(p.startswith("/api/v1/walker/network") for p in ps)
    assert any(p.startswith("/api/v1/walks") for p in ps)
    assert any(p.startswith("/api/v1/matching") for p in ps)


def test_legacy_routes_preserved():
    # ADITIVO: as rotas sem versão continuam existindo (não quebra apps distribuídos).
    ps = _paths()
    assert "/payments/quote" in ps
    assert "/auth/login" in ps


def test_admin_console_not_versioned_yet():
    # Rotas de console/admin interno ficam fora do /api/v1 nesta fase.
    ps = _paths()
    assert not any(p.startswith("/api/v1/admin") for p in ps)
