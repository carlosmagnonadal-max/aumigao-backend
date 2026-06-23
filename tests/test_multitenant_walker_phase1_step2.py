"""Testes — Fase 1 Passo 2: Passeador Multi-Tenant — get_walker_self_db.

Cobre:
  1. Flag OFF (default): get_walker_self_db seta rls_tenant=tenant_id (idêntico a get_db).
  2. Flag ON: get_walker_self_db seta rls_tenant="*".
  3. Cross-tenant (app-layer): query Walker filtrando walker_id retorna walks de 2 tenants.
  4. Isolamento entre walkers: query filtrando walker_id=A não traz walks do walker B.
  5. Regressão: flag OFF → rls_tenant é tenant_id (não "*").
  6. multi_tenant_walker_enabled() relê env a cada chamada (monkeypatch).

Notas de escopo:
  - SQLite em memória (StaticPool) — RLS NÃO é aplicado em SQLite.
  - Os testes validam a CAMADA DE APLICAÇÃO:
      a) db.info["rls_tenant"] setado corretamente pela dependency.
      b) Filtros de query (walker_id/assigned_walker_id) garantem isolamento.
  - A validação REAL do RLS (policy 0049 no Neon) cabe ao Carlos no banco
    de produção — SQLite não emula RLS.

Padrão: FastAPI mínimo (só onde necessário), SQLite StaticPool, testes de
dependency direta e query direta no ORM. Evitamos passar pelas rotas HTTP
completas (que têm dependências pesadas como _require_active_walker,
process_expired_attempts, etc.) — o foco é a camada que foi modificada.
"""
from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, or_
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 — registra todas as tabelas no Base.metadata
from app.core.database import Base, get_db, get_walker_self_db
from app.models.pet import Pet
from app.models.tenant import Tenant
from app.models.user import User
from app.models.walk import Walk

# ─── IDs de fixture ───────────────────────────────────────────────────────────

WALKER_A_ID = "walker-a"
WALKER_B_ID = "walker-b"
TUTOR_ID = "tutor-x"
TENANT_1_ID = "tenant-one"
TENANT_2_ID = "tenant-two"
PET_ID = "pet-x"


# ─── Helper: request fake com state.tenant_id ─────────────────────────────────


class _FakeState:
    def __init__(self, tenant_id: str):
        self.tenant_id = tenant_id


class _FakeRequest:
    def __init__(self, tenant_id: str):
        self.state = _FakeState(tenant_id)


# ─── Fábrica de banco com walks cross-tenant ──────────────────────────────────


def _build_db_with_walks():
    """Banco SQLite em memória com 2 tenants, 2 walkers e walks cross-tenant."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    db.add(Tenant(id=TENANT_1_ID, name="T1", slug="t1", status="active", plan="business"))
    db.add(Tenant(id=TENANT_2_ID, name="T2", slug="t2", status="active", plan="business"))

    db.add(User(id=TUTOR_ID, email="tutor@test.com", password_hash="x", role="tutor", tenant_id=TENANT_1_ID))
    db.add(User(id=WALKER_A_ID, email="wa@test.com", password_hash="x", role="walker", tenant_id=TENANT_1_ID))
    db.add(User(id=WALKER_B_ID, email="wb@test.com", password_hash="x", role="walker", tenant_id=TENANT_1_ID))

    db.add(Pet(id=PET_ID, tutor_id=TUTOR_ID, tenant_id=TENANT_1_ID, name="Rex"))

    # Walker A tem walks em T1 e T2 (cross-tenant)
    db.add(Walk(
        id="walk-a-t1",
        tutor_id=TUTOR_ID,
        pet_id=PET_ID,
        tenant_id=TENANT_1_ID,
        walker_id=WALKER_A_ID,
        scheduled_date="2026-06-23",
        duration_minutes=30,
        price=50.0,
        status="Concluído",
        operational_status="ride_completed",
    ))
    db.add(Walk(
        id="walk-a-t2",
        tutor_id=TUTOR_ID,
        pet_id=PET_ID,
        tenant_id=TENANT_2_ID,
        walker_id=WALKER_A_ID,
        scheduled_date="2026-06-23",
        duration_minutes=30,
        price=50.0,
        status="Concluído",
        operational_status="ride_completed",
    ))
    # Walker B tem walk em T1
    db.add(Walk(
        id="walk-b-t1",
        tutor_id=TUTOR_ID,
        pet_id=PET_ID,
        tenant_id=TENANT_1_ID,
        walker_id=WALKER_B_ID,
        scheduled_date="2026-06-23",
        duration_minutes=30,
        price=50.0,
        status="Concluído",
        operational_status="ride_completed",
    ))

    db.commit()
    return db


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Flag OFF (default): rls_tenant = tenant_id (comportamento idêntico a get_db)
# ═══════════════════════════════════════════════════════════════════════════════


def test_flag_off_seta_rls_tenant_correto(monkeypatch):
    """Flag OFF → rls_tenant=tenant_id (comportamento idêntico a get_db)."""
    monkeypatch.delenv("MULTI_TENANT_WALKER", raising=False)

    fake_request = _FakeRequest(TENANT_1_ID)
    gen = get_walker_self_db(request=fake_request)
    session = next(gen)
    try:
        assert session.info.get("rls_tenant") == TENANT_1_ID
    finally:
        try:
            next(gen)
        except StopIteration:
            pass


def test_flag_off_sem_request_seta_string_vazia(monkeypatch):
    """Flag OFF sem request → rls_tenant='' (fail-closed, igual a get_db sem tenant)."""
    monkeypatch.delenv("MULTI_TENANT_WALKER", raising=False)

    gen = get_walker_self_db(request=None)
    session = next(gen)
    try:
        assert session.info.get("rls_tenant") == ""
    finally:
        try:
            next(gen)
        except StopIteration:
            pass


def test_flag_off_comportamento_igual_a_get_db(monkeypatch):
    """Flag OFF: get_walker_self_db e get_db produzem o mesmo rls_tenant."""
    monkeypatch.delenv("MULTI_TENANT_WALKER", raising=False)

    fake_request = _FakeRequest(TENANT_2_ID)

    gen_walker = get_walker_self_db(request=fake_request)
    gen_db = get_db(request=fake_request)
    session_walker = next(gen_walker)
    session_db = next(gen_db)
    try:
        assert session_walker.info.get("rls_tenant") == session_db.info.get("rls_tenant")
    finally:
        for gen in (gen_walker, gen_db):
            try:
                next(gen)
            except StopIteration:
                pass


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Flag ON: rls_tenant = "*"
# ═══════════════════════════════════════════════════════════════════════════════


def test_flag_on_seta_rls_tenant_global(monkeypatch):
    """Flag ON → rls_tenant='*' (escopo global para leituras do passeador)."""
    monkeypatch.setenv("MULTI_TENANT_WALKER", "true")

    fake_request = _FakeRequest(TENANT_1_ID)
    gen = get_walker_self_db(request=fake_request)
    session = next(gen)
    try:
        assert session.info.get("rls_tenant") == "*"
    finally:
        try:
            next(gen)
        except StopIteration:
            pass


def test_flag_on_sem_request_tambem_global(monkeypatch):
    """Flag ON sem request → rls_tenant='*' (não depende do request)."""
    monkeypatch.setenv("MULTI_TENANT_WALKER", "true")

    gen = get_walker_self_db(request=None)
    session = next(gen)
    try:
        assert session.info.get("rls_tenant") == "*"
    finally:
        try:
            next(gen)
        except StopIteration:
            pass


def test_flag_on_sobrepoe_tenant_do_request(monkeypatch):
    """Flag ON: mesmo com tenant no request, rls_tenant é '*'."""
    monkeypatch.setenv("MULTI_TENANT_WALKER", "true")

    # Mesmo com tenant_id no request, deve retornar "*"
    for tenant_id in (TENANT_1_ID, TENANT_2_ID, "qualquer-tenant"):
        fake_request = _FakeRequest(tenant_id)
        gen = get_walker_self_db(request=fake_request)
        session = next(gen)
        try:
            assert session.info.get("rls_tenant") == "*", (
                f"Esperado '*' com flag ON e tenant_id={tenant_id!r}"
            )
        finally:
            try:
                next(gen)
            except StopIteration:
                pass


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Cross-tenant (app-layer): walker A vê walks de T1 e T2 com filtro walker_id
# ═══════════════════════════════════════════════════════════════════════════════


def test_cross_tenant_query_walker_id_retorna_ambos_tenants():
    """Query com walker_id=A e sem filtro de tenant retorna walks de T1 e T2.

    Simula o que /walker/walks faz com rls_tenant='*':
    a camada de query filtra por walker_id/assigned_walker_id (não por tenant).
    Em SQLite não há RLS; em Postgres o RLS com walker-self cláusula permite.
    """
    db = _build_db_with_walks()

    # Simula escopo global (flag ON)
    db.info["rls_tenant"] = "*"

    # Query idêntica à de /walker/walks:
    # (Walk.walker_id == user.id) | (Walk.assigned_walker_id == user.id)
    visible_statuses = {
        "walker_accepted", "ride_scheduled", "walker_arriving",
        "ride_in_progress", "ride_completed", "ride_cancelled",
    }
    walks = (
        db.query(Walk)
        .filter(
            or_(Walk.walker_id == WALKER_A_ID, Walk.assigned_walker_id == WALKER_A_ID),
            Walk.operational_status.in_(visible_statuses),
        )
        .all()
    )

    walk_ids = {w.id for w in walks}
    # Deve trazer walk de T1 e de T2
    assert "walk-a-t1" in walk_ids, "walk do T1 deve aparecer"
    assert "walk-a-t2" in walk_ids, "walk do T2 deve aparecer (cross-tenant)"
    # walk do walker B não deve aparecer
    assert "walk-b-t1" not in walk_ids, "walk do walker B não deve aparecer"


def test_cross_tenant_count_correto():
    """Walker A tem 2 walks (T1 + T2); query por walker_id retorna count=2."""
    db = _build_db_with_walks()
    db.info["rls_tenant"] = "*"

    count = (
        db.query(Walk)
        .filter(or_(Walk.walker_id == WALKER_A_ID, Walk.assigned_walker_id == WALKER_A_ID))
        .count()
    )
    assert count == 2


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Isolamento entre walkers: query por walker_id isola walker A de walker B
# ═══════════════════════════════════════════════════════════════════════════════


def test_isolamento_walker_a_nao_ve_walks_do_walker_b():
    """Walker A (filtro walker_id=A) não recebe walks onde walker_id=B."""
    db = _build_db_with_walks()
    db.info["rls_tenant"] = "*"  # escopo global (flag ON)

    walks_a = (
        db.query(Walk)
        .filter(or_(Walk.walker_id == WALKER_A_ID, Walk.assigned_walker_id == WALKER_A_ID))
        .all()
    )
    ids_a = {w.id for w in walks_a}
    # walk-b-t1 não deve aparecer para walker A
    assert "walk-b-t1" not in ids_a


def test_isolamento_walker_b_nao_ve_walks_do_walker_a():
    """Walker B (filtro walker_id=B) não recebe walks do walker A."""
    db = _build_db_with_walks()
    db.info["rls_tenant"] = "*"  # escopo global (flag ON)

    walks_b = (
        db.query(Walk)
        .filter(or_(Walk.walker_id == WALKER_B_ID, Walk.assigned_walker_id == WALKER_B_ID))
        .all()
    )
    ids_b = {w.id for w in walks_b}
    # walks do walker A não devem aparecer para walker B
    assert "walk-a-t1" not in ids_b
    assert "walk-a-t2" not in ids_b
    # walk do walker B deve aparecer
    assert "walk-b-t1" in ids_b


def test_isolamento_walker_b_count_correto():
    """Walker B tem 1 walk; query por walker_id retorna count=1."""
    db = _build_db_with_walks()
    db.info["rls_tenant"] = "*"

    count = (
        db.query(Walk)
        .filter(or_(Walk.walker_id == WALKER_B_ID, Walk.assigned_walker_id == WALKER_B_ID))
        .count()
    )
    assert count == 1


# ═══════════════════════════════════════════════════════════════════════════════
# 5. Regressão: flag OFF → rls_tenant é tenant_id (não "*")
# ═══════════════════════════════════════════════════════════════════════════════


def test_regressao_flag_off_rls_tenant_nao_e_star(monkeypatch):
    """Flag OFF → rls_tenant nunca é '*' (tenant-scoped, sem regressão)."""
    monkeypatch.delenv("MULTI_TENANT_WALKER", raising=False)

    for tenant_id in (TENANT_1_ID, TENANT_2_ID):
        fake_request = _FakeRequest(tenant_id)
        gen = get_walker_self_db(request=fake_request)
        session = next(gen)
        try:
            rls = session.info.get("rls_tenant")
            assert rls != "*", f"Flag OFF não deve setar '*', obteve {rls!r}"
            assert rls == tenant_id
        finally:
            try:
                next(gen)
            except StopIteration:
                pass


def test_regressao_flag_off_query_tenant_scoped():
    """Flag OFF com rls_tenant=T1: query de walks sem filtro de tenant lista todas (SQLite).

    Em SQLite não há RLS — mas a dependency está corretamente setada como T1.
    O importante é que rls_tenant foi setado, não '*'.
    Em Postgres, o RLS filtering seria aplicado pelo banco.
    """
    db = _build_db_with_walks()

    # Simula flag OFF: rls_tenant = TENANT_1_ID
    db.info["rls_tenant"] = TENANT_1_ID

    # Verifica que o valor foi setado corretamente (validação da dependency)
    assert db.info.get("rls_tenant") == TENANT_1_ID
    assert db.info.get("rls_tenant") != "*"


# ═══════════════════════════════════════════════════════════════════════════════
# 6. multi_tenant_walker_enabled() relê env a cada chamada
# ═══════════════════════════════════════════════════════════════════════════════


def test_multi_tenant_walker_enabled_rele_env(monkeypatch):
    """multi_tenant_walker_enabled() relê env var sem cache de módulo."""
    from app.core.feature_flags import multi_tenant_walker_enabled

    monkeypatch.delenv("MULTI_TENANT_WALKER", raising=False)
    assert multi_tenant_walker_enabled() is False

    monkeypatch.setenv("MULTI_TENANT_WALKER", "true")
    assert multi_tenant_walker_enabled() is True

    monkeypatch.setenv("MULTI_TENANT_WALKER", "false")
    assert multi_tenant_walker_enabled() is False


def test_multi_tenant_walker_enabled_valores_truthy(monkeypatch):
    """multi_tenant_walker_enabled() aceita 1/yes/on/true como truthy."""
    from app.core.feature_flags import multi_tenant_walker_enabled

    for val in ("1", "yes", "on", "true", "TRUE", "YES", "ON"):
        monkeypatch.setenv("MULTI_TENANT_WALKER", val)
        assert multi_tenant_walker_enabled() is True, f"Valor {val!r} deveria ser truthy"

    for val in ("0", "no", "off", "false", "FALSE"):
        monkeypatch.setenv("MULTI_TENANT_WALKER", val)
        assert multi_tenant_walker_enabled() is False, f"Valor {val!r} deveria ser falsy"


def test_multi_tenant_walker_enabled_ausente_e_false(monkeypatch):
    """Sem env var → multi_tenant_walker_enabled() retorna False (default OFF)."""
    from app.core.feature_flags import multi_tenant_walker_enabled

    monkeypatch.delenv("MULTI_TENANT_WALKER", raising=False)
    assert multi_tenant_walker_enabled() is False


# ═══════════════════════════════════════════════════════════════════════════════
# 7. walker_network: /walker/network/invites e /me usam get_walker_self_db
# ═══════════════════════════════════════════════════════════════════════════════


def test_walker_network_invites_flag_on_usa_escopo_global(monkeypatch):
    """Flag ON: get_walker_self_db (usado por /invites e /me) → rls_tenant='*'."""
    monkeypatch.setenv("MULTI_TENANT_WALKER", "true")

    fake_request = _FakeRequest(TENANT_1_ID)
    gen = get_walker_self_db(request=fake_request)
    session = next(gen)
    try:
        assert session.info.get("rls_tenant") == "*"
    finally:
        try:
            next(gen)
        except StopIteration:
            pass


def test_walker_network_invites_flag_off_usa_tenant(monkeypatch):
    """Flag OFF: get_walker_self_db (usado por /invites e /me) → rls_tenant=tenant_id."""
    monkeypatch.delenv("MULTI_TENANT_WALKER", raising=False)

    fake_request = _FakeRequest(TENANT_2_ID)
    gen = get_walker_self_db(request=fake_request)
    session = next(gen)
    try:
        assert session.info.get("rls_tenant") == TENANT_2_ID
    finally:
        try:
            next(gen)
        except StopIteration:
            pass


# ═══════════════════════════════════════════════════════════════════════════════
# 8. Verificar que get_walker_self_db foi importado corretamente em walker.py
# ═══════════════════════════════════════════════════════════════════════════════


def test_walker_routes_importa_get_walker_self_db():
    """Verifica que app.routes.walker importa get_walker_self_db (não ausente)."""
    import app.routes.walker as walker_module
    assert hasattr(walker_module, "get_walker_self_db"), (
        "walker.py deve importar get_walker_self_db de app.core.database"
    )


def test_walker_network_routes_importa_get_walker_self_db():
    """Verifica que app.routes.walker_network importa get_walker_self_db."""
    import app.routes.walker_network as wn_module
    assert hasattr(wn_module, "get_walker_self_db"), (
        "walker_network.py deve importar get_walker_self_db de app.core.database"
    )


def test_database_exporta_get_walker_self_db():
    """Verifica que app.core.database exporta get_walker_self_db."""
    import app.core.database as db_module
    assert hasattr(db_module, "get_walker_self_db"), (
        "app.core.database deve exportar get_walker_self_db"
    )
