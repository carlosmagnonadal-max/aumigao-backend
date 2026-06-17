"""test-T11 — snapshot anti-drift da SUPERFÍCIE DE ROTAS (método + path).

Objetivo: pegar remoção/renomeação ACIDENTAL de endpoint (regressão que quebra
apps distribuídos) e forçar uma revisão consciente ao ADICIONAR rotas. Não é
frágil a mudanças de schema/campos — observa apenas o conjunto (método, path).

Atualizar após uma mudança INTENCIONAL de rotas (rodar na pasta backend/):
    python -m tests.test_route_surface_snapshot    # regenera o JSON
"""
import json
import sys
from pathlib import Path

# Permite rodar como script (regeneração) com a raiz do backend no sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.main import app

SNAPSHOT_PATH = Path(__file__).parent / "route_surface_snapshot.json"


def current_route_surface() -> set[str]:
    surface: set[str] = set()
    for route in app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if not path or not methods:
            continue
        for method in methods:
            if method in ("HEAD", "OPTIONS"):  # auto-adicionados pelo framework
                continue
            surface.add(f"{method} {path}")
    return surface


def test_route_surface_matches_snapshot():
    expected = set(json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8")))
    current = current_route_surface()

    removed = sorted(expected - current)
    added = sorted(current - expected)

    assert not removed, (
        "Rotas REMOVIDAS ou renomeadas (possível regressão que quebra apps em uso): "
        f"{removed}"
    )
    assert not added, (
        "Novas rotas detectadas. Se for intencional, regenere o snapshot com "
        "`python tests/test_route_surface_snapshot.py`. Novas rotas: "
        f"{added}"
    )


if __name__ == "__main__":
    data = sorted(current_route_surface())
    SNAPSHOT_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"snapshot atualizado: {len(data)} rotas em {SNAPSHOT_PATH.name}")
