"""Testes — Fase 1 Passo 5: Walker Exclusivity Service + guards de matching.

Princípio de F1: exclusive_tenant_id é SEMPRE NULL na prod (nenhuma UX seta).
Portanto os guards são DORMENTES — passam reto para todos os walkers.
Os testes exercitam tanto o caminho dormente (NULL) quanto o caminho de
enforcement (setando exclusive_tenant_id manualmente no banco), provando que
o código está correto e pronto para F2.

Cobre:
  1. Dormente/regressão: pool de matching inalterado com exclusive_tenant_id NULL.
  2. Exclusivo exclui de outro tenant: walker exclusivo de T1 não aparece no pool de T2.
  3. assert_walker_link_allowed: walker exclusivo de T1 → 409 ao vincular a T2;
     tenant_exclusive com vínculo ativo em outro tenant → 409.
  4. Aceite de convite bloqueado: exclusivo de T1 tenta aceitar convite de T2 → 409;
     convite do próprio T1 → ok.
  5. accept_walk: walker exclusivo de T1 não é elegível para walk de T2.
  6. set_walker_exclusive / release_walker_exclusive.

Padrão: SQLite StaticPool, sem FastAPI TestClient (serviços testados diretamente).
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 — garante que todas as tabelas estão no Base.metadata
from app.core.database import Base
from app.models.tenant import Tenant
from app.models.tenant_walker_access import TenantWalkerAccess
from app.models.user import User
from app.models.walker_network_profile import WalkerNetworkProfile
from app.models.walker_profile import WalkerProfile
from app.services.walker_exclusivity_service import (
    assert_walker_link_allowed,
    get_exclusive_tenant_id,
    release_walker_exclusive,
    set_walker_exclusive,
    walker_exclusivity_ok,
)
from app.services.walker_network_matching_service import (
    get_tenant_eligible_walker_ids,
    is_walker_eligible_for_tenant,
)

# ── IDs de fixture ─────────────────────────────────────────────────────────────

T1 = "tenant-excl-t1"
T2 = "tenant-excl-t2"
W1 = "walker-excl-w1"
W2 = "walker-excl-w2"


# ── Fábrica de banco em memória ────────────────────────────────────────────────


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    session = Session()
    yield session
    session.close()


# ── Helpers de seed ────────────────────────────────────────────────────────────


def _add_tenant(db, tid: str) -> Tenant:
    t = Tenant(id=tid, name=tid, slug=tid, status="active")
    db.add(t)
    return t


def _add_walker(db, wid: str) -> User:
    u = User(id=wid, email=f"{wid}@x.com", password_hash="x", role="walker", is_active=True)
    wp = WalkerProfile(
        id=f"wp-{wid}",
        user_id=wid,
        full_name=wid,
        status="active",
        active_as_walker=True,
    )
    db.add_all([u, wp])
    return u


def _add_network_profile(db, wid: str, exclusive_tenant_id: str | None = None) -> WalkerNetworkProfile:
    p = WalkerNetworkProfile(walker_user_id=wid, exclusive_tenant_id=exclusive_tenant_id)
    db.add(p)
    return p


def _add_access(db, wid: str, tid: str, status: str = "active", access_type: str = "shared_network") -> TenantWalkerAccess:
    a = TenantWalkerAccess(
        id=f"twa-{wid}-{tid}",
        tenant_id=tid,
        walker_user_id=wid,
        status=status,
        access_type=access_type,
    )
    db.add(a)
    return a


# ══════════════════════════════════════════════════════════════════════════════
# 1. Dormente / regressão
#    Com exclusive_tenant_id NULL (default F1), o pool de matching é inalterado.
# ══════════════════════════════════════════════════════════════════════════════


def test_dormant_null_exclusive_get_eligible_includes_walker(db):
    """Walker com exclusive_tenant_id NULL aparece no pool de T1 (comportamento F1)."""
    _add_tenant(db, T1)
    _add_walker(db, W1)
    _add_access(db, W1, T1)
    _add_network_profile(db, W1, exclusive_tenant_id=None)
    db.commit()

    pool = get_tenant_eligible_walker_ids(db, T1)
    assert W1 in pool


def test_dormant_null_exclusive_is_eligible_true(db):
    """is_walker_eligible_for_tenant retorna True com exclusive_tenant_id NULL."""
    _add_tenant(db, T1)
    _add_walker(db, W1)
    _add_access(db, W1, T1)
    _add_network_profile(db, W1, exclusive_tenant_id=None)
    db.commit()

    assert is_walker_eligible_for_tenant(db, T1, W1) is True


def test_dormant_no_network_profile_still_eligible(db):
    """Walker SEM WalkerNetworkProfile (LEFT JOIN) continua elegível — zero regressão."""
    _add_tenant(db, T1)
    _add_walker(db, W1)
    _add_access(db, W1, T1)
    # SEM _add_network_profile — simula todos os walkers pré-F1 que não têm profile
    db.commit()

    assert W1 in get_tenant_eligible_walker_ids(db, T1)
    assert is_walker_eligible_for_tenant(db, T1, W1) is True


def test_dormant_two_walkers_both_in_pool_with_null_exclusive(db):
    """Dois walkers com exclusive NULL: ambos no pool (comportamento anterior preservado)."""
    _add_tenant(db, T1)
    _add_walker(db, W1)
    _add_walker(db, W2)
    _add_access(db, W1, T1)
    _add_access(db, W2, T1)
    # Sem network profile = NULL implícito
    db.commit()

    pool = get_tenant_eligible_walker_ids(db, T1)
    assert W1 in pool
    assert W2 in pool


# ══════════════════════════════════════════════════════════════════════════════
# 2. Exclusivo exclui de OUTRO tenant
#    (F2: exclusive_tenant_id setado manualmente)
# ══════════════════════════════════════════════════════════════════════════════


def test_exclusive_walker_excluded_from_other_tenant_pool(db):
    """Walker exclusivo de T1 NÃO aparece no pool de T2."""
    _add_tenant(db, T1)
    _add_tenant(db, T2)
    _add_walker(db, W1)
    # W1 tem acesso a AMBOS os tenants (TWA active em T1 e T2)
    _add_access(db, W1, T1)
    _add_access(db, W1, T2)
    # Profile diz que é exclusivo de T1
    _add_network_profile(db, W1, exclusive_tenant_id=T1)
    db.commit()

    pool_t1 = get_tenant_eligible_walker_ids(db, T1)
    pool_t2 = get_tenant_eligible_walker_ids(db, T2)

    assert W1 in pool_t1, "Exclusivo de T1 deve aparecer no pool de T1"
    assert W1 not in pool_t2, "Exclusivo de T1 NÃO deve aparecer no pool de T2"


def test_exclusive_walker_not_eligible_for_other_tenant(db):
    """is_walker_eligible_for_tenant: exclusivo de T1 → False para T2."""
    _add_tenant(db, T1)
    _add_tenant(db, T2)
    _add_walker(db, W1)
    _add_access(db, W1, T1)
    _add_access(db, W1, T2)
    _add_network_profile(db, W1, exclusive_tenant_id=T1)
    db.commit()

    assert is_walker_eligible_for_tenant(db, T1, W1) is True
    assert is_walker_eligible_for_tenant(db, T2, W1) is False


def test_non_exclusive_walker_visible_to_both_tenants(db):
    """Walker não exclusivo (NULL) com acesso nos dois tenants aparece em ambos."""
    _add_tenant(db, T1)
    _add_tenant(db, T2)
    _add_walker(db, W1)
    _add_access(db, W1, T1)
    _add_access(db, W1, T2)
    # SEM network profile → exclusive = NULL
    db.commit()

    assert W1 in get_tenant_eligible_walker_ids(db, T1)
    assert W1 in get_tenant_eligible_walker_ids(db, T2)


# ══════════════════════════════════════════════════════════════════════════════
# 3. assert_walker_link_allowed
# ══════════════════════════════════════════════════════════════════════════════


def test_assert_link_allowed_exclusive_of_another_raises_409(db):
    """Walker exclusivo de T1 → 409 ao tentar vincular a T2."""
    _add_tenant(db, T1)
    _add_tenant(db, T2)
    _add_walker(db, W1)
    _add_access(db, W1, T1)
    _add_network_profile(db, W1, exclusive_tenant_id=T1)
    db.commit()

    with pytest.raises(HTTPException) as exc_info:
        assert_walker_link_allowed(db, W1, T2, "shared_network")
    assert exc_info.value.status_code == 409
    assert "exclusivo" in exc_info.value.detail.lower()


def test_assert_link_allowed_exclusive_of_same_tenant_ok(db):
    """Walker exclusivo de T1 pode ter o vínculo com T1 atualizado (mesmo tenant → ok)."""
    _add_tenant(db, T1)
    _add_walker(db, W1)
    _add_access(db, W1, T1)
    _add_network_profile(db, W1, exclusive_tenant_id=T1)
    db.commit()

    # Não deve levantar exceção
    assert_walker_link_allowed(db, W1, T1, "shared_network")


def test_assert_link_allowed_tenant_exclusive_with_active_other_raises_409(db):
    """Tornar walker tenant_exclusive quando tem vínculo active em outro tenant → 409."""
    _add_tenant(db, T1)
    _add_tenant(db, T2)
    _add_walker(db, W1)
    _add_access(db, W1, T2, status="active")  # vínculo ativo em T2
    # SEM network profile → exclusive = NULL (não exclusivo de nenhum)
    db.commit()

    with pytest.raises(HTTPException) as exc_info:
        assert_walker_link_allowed(db, W1, T1, "tenant_exclusive")
    assert exc_info.value.status_code == 409
    assert "vínculos ativos" in exc_info.value.detail.lower()


def test_assert_link_allowed_tenant_exclusive_no_other_active_ok(db):
    """Tornar walker tenant_exclusive sem vínculos ativos em outros tenants → ok."""
    _add_tenant(db, T1)
    _add_tenant(db, T2)
    _add_walker(db, W1)
    # Vínculo em T2 mas com status revoked (não ativo)
    _add_access(db, W1, T2, status="revoked")
    db.commit()

    # Não deve levantar exceção
    assert_walker_link_allowed(db, W1, T1, "tenant_exclusive")


def test_assert_link_allowed_null_exclusive_shared_ok(db):
    """Walker sem exclusividade (NULL) pode ser vinculado a qualquer tenant."""
    _add_tenant(db, T1)
    _add_tenant(db, T2)
    _add_walker(db, W1)
    db.commit()

    # Não deve levantar exceção para nenhum dos dois tenants
    assert_walker_link_allowed(db, W1, T1, "shared_network")
    assert_walker_link_allowed(db, W1, T2, "shared_network")


# ══════════════════════════════════════════════════════════════════════════════
# 4. Aceite de convite — walker_exclusivity_ok
# ══════════════════════════════════════════════════════════════════════════════


def test_walker_exclusivity_ok_null_always_true(db):
    """walker_exclusivity_ok: walker sem exclusividade → True para qualquer tenant."""
    _add_walker(db, W1)
    db.commit()

    assert walker_exclusivity_ok(db, W1, T1) is True
    assert walker_exclusivity_ok(db, W1, T2) is True


def test_walker_exclusivity_ok_exclusive_of_same_tenant_true(db):
    """walker_exclusivity_ok: exclusivo de T1 → True para T1."""
    _add_tenant(db, T1)
    _add_walker(db, W1)
    _add_network_profile(db, W1, exclusive_tenant_id=T1)
    db.commit()

    assert walker_exclusivity_ok(db, W1, T1) is True


def test_walker_exclusivity_ok_exclusive_of_other_tenant_false(db):
    """walker_exclusivity_ok: exclusivo de T1 → False para T2."""
    _add_tenant(db, T1)
    _add_walker(db, W1)
    _add_network_profile(db, W1, exclusive_tenant_id=T1)
    db.commit()

    assert walker_exclusivity_ok(db, W1, T2) is False


def test_invite_accept_blocked_for_exclusive_of_other_tenant(db):
    """Convite de T2 bloqueado (409) para walker exclusivo de T1.

    Simula a lógica do guard em _respond_to_invite diretamente via
    walker_exclusivity_ok (o endpoint checa isso antes de setar status).
    """
    _add_tenant(db, T1)
    _add_tenant(db, T2)
    _add_walker(db, W1)
    _add_network_profile(db, W1, exclusive_tenant_id=T1)
    db.commit()

    # Exclusivo de T1 não pode aceitar convite de T2
    ok_for_t2 = walker_exclusivity_ok(db, W1, T2)
    assert ok_for_t2 is False, "Deveria retornar False para T2 (exclusivo de T1)"


def test_invite_accept_allowed_for_own_tenant(db):
    """Convite do próprio tenant (T1) ok para walker exclusivo de T1."""
    _add_tenant(db, T1)
    _add_walker(db, W1)
    _add_network_profile(db, W1, exclusive_tenant_id=T1)
    db.commit()

    assert walker_exclusivity_ok(db, W1, T1) is True


# ══════════════════════════════════════════════════════════════════════════════
# 5. accept_walk — via is_walker_eligible_for_tenant
#    (accept_walk já usa is_walker_eligible_for_tenant; testar a função diretamente
#     para cobrir o path exclusividade + matching sem montar o stack operacional)
# ══════════════════════════════════════════════════════════════════════════════


def test_accept_walk_exclusive_t1_not_eligible_for_t2(db):
    """Walker exclusivo de T1 não é elegível para walk de T2 (via is_walker_eligible_for_tenant)."""
    _add_tenant(db, T1)
    _add_tenant(db, T2)
    _add_walker(db, W1)
    # W1 tem acesso ativo em ambos, mas é exclusivo de T1
    _add_access(db, W1, T1, status="active")
    _add_access(db, W1, T2, status="active")
    _add_network_profile(db, W1, exclusive_tenant_id=T1)
    db.commit()

    assert is_walker_eligible_for_tenant(db, T1, W1) is True
    assert is_walker_eligible_for_tenant(db, T2, W1) is False


def test_accept_walk_exclusive_t1_eligible_for_t1(db):
    """Walker exclusivo de T1 é elegível para walk de T1."""
    _add_tenant(db, T1)
    _add_walker(db, W1)
    _add_access(db, W1, T1, status="active")
    _add_network_profile(db, W1, exclusive_tenant_id=T1)
    db.commit()

    assert is_walker_eligible_for_tenant(db, T1, W1) is True


# ══════════════════════════════════════════════════════════════════════════════
# 6. set_walker_exclusive / release_walker_exclusive
# ══════════════════════════════════════════════════════════════════════════════


def test_set_walker_exclusive_creates_profile_if_missing(db):
    """set_walker_exclusive cria WalkerNetworkProfile se não existir."""
    _add_tenant(db, T1)
    _add_walker(db, W1)
    db.commit()

    set_walker_exclusive(db, W1, T1)
    db.commit()

    assert get_exclusive_tenant_id(db, W1) == T1


def test_set_walker_exclusive_updates_existing_profile(db):
    """set_walker_exclusive atualiza profile existente sem criar duplicata."""
    _add_tenant(db, T1)
    _add_walker(db, W1)
    _add_network_profile(db, W1, exclusive_tenant_id=None)
    db.commit()

    set_walker_exclusive(db, W1, T1)
    db.commit()

    assert get_exclusive_tenant_id(db, W1) == T1


def test_set_walker_exclusive_with_active_other_link_raises_409(db):
    """set_walker_exclusive levanta 409 se há vínculo ativo com outro tenant."""
    _add_tenant(db, T1)
    _add_tenant(db, T2)
    _add_walker(db, W1)
    _add_access(db, W1, T2, status="active")  # vínculo ativo em T2
    db.commit()

    with pytest.raises(HTTPException) as exc_info:
        set_walker_exclusive(db, W1, T1)
    assert exc_info.value.status_code == 409


def test_release_walker_exclusive_clears_field(db):
    """release_walker_exclusive zera exclusive_tenant_id."""
    _add_tenant(db, T1)
    _add_walker(db, W1)
    _add_network_profile(db, W1, exclusive_tenant_id=T1)
    db.commit()

    assert get_exclusive_tenant_id(db, W1) == T1

    release_walker_exclusive(db, W1)
    db.commit()

    assert get_exclusive_tenant_id(db, W1) is None


def test_release_walker_exclusive_noop_if_no_profile(db):
    """release_walker_exclusive não falha se não há profile."""
    _add_walker(db, W1)
    db.commit()

    # Não deve levantar exceção
    release_walker_exclusive(db, W1)
    db.commit()

    assert get_exclusive_tenant_id(db, W1) is None


def test_get_exclusive_tenant_id_returns_none_when_no_profile(db):
    """get_exclusive_tenant_id retorna None quando não existe WalkerNetworkProfile."""
    _add_walker(db, W1)
    db.commit()

    assert get_exclusive_tenant_id(db, W1) is None
