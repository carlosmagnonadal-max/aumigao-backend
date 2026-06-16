"""api-T1 — o enum e a fonte unica de verdade dos defaults/sets de schema.

Optamos por NAO tipar os campos de ENTRADA de invite/acesso como StrEnum: a
rota walker_network valida o status manualmente e devolve 400 em valor
invalido; tipar como StrEnum mudaria esse contrato para 422 (Pydantic). Em vez
disso, o schema referencia app.enums.TenantWalkerAccessStatus como fonte do
default e o set legado TENANT_WALKER_ACCESS_STATUSES espelha exatamente o enum.
"""
from app.enums import TenantWalkerAccessStatus
from app.schemas.walker_network import (
    TENANT_WALKER_ACCESS_STATUSES,
    TenantWalkerAccessCreate,
)


def test_enum_mirrors_the_legacy_string_set():
    # O enum cobre exatamente o set ja usado pela rota / route guard.
    assert {m.value for m in TenantWalkerAccessStatus} == TENANT_WALKER_ACCESS_STATUSES


def test_create_schema_default_comes_from_enum():
    # Default do schema = valor canonico do enum (compat: continua str crua).
    obj = TenantWalkerAccessCreate(walker_user_id="w1")
    assert obj.status == TenantWalkerAccessStatus.ACTIVE
    assert obj.status == "active"


def test_create_schema_still_accepts_arbitrary_string_input():
    # Comportamento preservado: validacao de status fica na ROTA (400), nao no schema.
    obj = TenantWalkerAccessCreate(walker_user_id="w1", status="whatever")
    assert obj.status == "whatever"
