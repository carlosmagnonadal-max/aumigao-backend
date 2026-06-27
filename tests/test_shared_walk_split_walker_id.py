import inspect
from app.services import shared_walk_service

def test_shared_walk_passes_walker_id_to_build_split():
    """O call site de passeio compartilhado deve passar walker_id para build_payment_split,
    senão a taxa de rede (18/10%) nunca é aplicada mesmo com PRICING_V2_ENABLED=True."""
    src = inspect.getsource(shared_walk_service)
    assert "build_payment_split(" in src
    call = src[src.index("build_payment_split("):]
    call = call[: call.index(")") + 1]
    assert "walker_id" in call, f"walker_id ausente na chamada: {call}"
