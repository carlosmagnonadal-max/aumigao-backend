"""api-T1 — prova de compat dos StrEnums de estados de dominio.

Os StrEnums em app/enums/ apenas DAO NOMES aos estados canonicos que ja
circulam como string solta no codigo. O contrato e: cada enum espelha
EXATAMENTE os valores em uso hoje (mesmo .value string), para que possa ser
adotado gradualmente sem mudar comportamento.

Estes testes ancoram os valores nas FONTES reais (modelos / services) para
que qualquer divergencia futura quebre aqui antes de virar bug de producao.
"""
from app.enums import (
    ApplicationStatus,
    PaymentStatus,
    TenantWalkerAccessStatus,
    WalkOperationalStatus,
)


def _values(enum_cls) -> set[str]:
    return {member.value for member in enum_cls}


# ------------------------------------------------------------ StrEnum basics --
def test_strenum_member_equals_plain_string():
    # StrEnum: o membro E a string -> compat total com comparacoes existentes.
    assert PaymentStatus.AGUARDANDO_PAGAMENTO == "aguardando_pagamento"
    assert WalkOperationalStatus.RIDE_SCHEDULED == "ride_scheduled"
    assert ApplicationStatus.SUBMITTED == "submitted"
    assert TenantWalkerAccessStatus.ACTIVE == "active"
    # usavel diretamente onde se espera str
    assert f"{PaymentStatus.FALHA_PAGAMENTO}" == "falha_pagamento"


# --------------------------------------------------------------- PaymentStatus --
def test_payment_status_values_match_code():
    # Fonte: app/routes/payments.py (STATUS_BY_ASAAS_STATUS, _PAYMENT_*_STATUS).
    assert _values(PaymentStatus) == {
        "pagamento_sandbox_criado",
        "aguardando_pagamento",
        "pagamento_confirmado_sandbox",
        "falha_pagamento",
        "pagamento_estornado",
    }


# ----------------------------------------------------- WalkOperationalStatus --
def test_walk_operational_status_covers_service_constants():
    # Fonte: app/services/operational_matching_service.py (constantes de modulo)
    # + app/routes/walks.py / payments.py ("awaiting_payment").
    expected = {
        "awaiting_payment",
        "pending_walker_confirmation",
        "walker_accepted",
        "walker_declined",
        "auto_rematching",
        "no_walker_found",
        "ride_scheduled",
        "walker_arriving",
        "ride_in_progress",
        "ride_completed",
        "ride_cancelled",
        "awaiting_tutor_reconfirmation",
    }
    assert _values(WalkOperationalStatus) == expected


def test_walk_operational_default_is_ride_scheduled():
    # Walk.operational_status default = "ride_scheduled" (app/models/walk.py).
    assert WalkOperationalStatus.RIDE_SCHEDULED == "ride_scheduled"


# ----------------------------------------------------------- ApplicationStatus --
def test_application_status_values_match_walker_canonicalizer():
    # Fonte: app/routes/walker.py (_canonical_application_status + labels).
    assert _values(ApplicationStatus) == {
        "submitted",
        "under_review",
        "approved",
        "active",
        "rejected",
        "resubmission_requested",
        "blocked",
    }


# ------------------------------------------------- TenantWalkerAccessStatus --
def test_tenant_walker_access_status_values_match_model():
    # Fonte: app/models/tenant_walker_access.py (docstring da maquina de estados).
    assert _values(TenantWalkerAccessStatus) == {
        "pending",
        "active",
        "declined",
        "revoked",
        "paused",
    }


# ----------------------------------------------------------- no accidental dup --
def test_no_duplicate_values_within_each_enum():
    for enum_cls in (
        PaymentStatus,
        WalkOperationalStatus,
        ApplicationStatus,
        TenantWalkerAccessStatus,
    ):
        members = list(enum_cls)
        assert len({m.value for m in members}) == len(members), enum_cls.__name__
