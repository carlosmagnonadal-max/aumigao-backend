"""api-T5 — /api/v1 versionado (aditivo). Os routers de NEGÓCIO são montados também
sob /api/v1, sem remover as rotas atuais (apps em uso não quebram). Console/admin
interno fica fora por ora."""
from app.main import app


def _collect_paths(route_list, result: set[str]) -> None:
    """Percorre a lista de rotas recursivamente, compondo o path completo.

    FastAPI >= 0.136 usa _IncludedRouter (lazy wrapper) ao inves de achatar as
    rotas diretamente em app.routes. Dois caminhos:

    1. _IncludedRouter com effective_route_contexts() — retorna contextos com o
       path ja composto (incluindo prefixos de roteadores pai). Usado para capturar
       rotas aninhadas como /api/v1/payments/quote.
    2. APIRoute normal (ou rota sem original_router) — usa route.path diretamente.
    """
    for route in route_list:
        # FastAPI >= 0.136: _IncludedRouter tem effective_route_contexts()
        # que retorna os paths COMPOSTOS (com prefixo do router pai incluido).
        erc = getattr(route, "effective_route_contexts", None)
        if erc is not None and callable(erc):
            try:
                for ctx in erc():
                    ctx_path = getattr(ctx, "path", None)
                    if ctx_path:
                        result.add(ctx_path)
            except Exception:
                pass
            continue

        # Rota simples (APIRoute, Route): usa path direto.
        path = getattr(route, "path", None)
        if path:
            result.add(path)

        # Fallback: recursa em original_router.routes se existir.
        orig = getattr(route, "original_router", None)
        if orig is not None:
            _collect_paths(getattr(orig, "routes", []), result)


def _paths() -> set[str]:
    result: set[str] = set()
    _collect_paths(app.routes, result)
    return result


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
