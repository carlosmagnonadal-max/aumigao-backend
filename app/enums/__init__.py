"""StrEnums dos estados canonicos de dominio (api-T1).

Hoje esses estados circulam como string solta em modelos, services e rotas.
Estes StrEnums dao NOME a esses valores SEM mudar comportamento: como
`enum.StrEnum`, cada membro E identico a propria string (``StatusX.FOO ==
"foo"`` e ``f"{StatusX.FOO}" == "foo"``), entao podem ser adotados de forma
incremental e expostos no OpenAPI sem reescrever os call sites existentes.

IMPORTANTE: os valores aqui DEVEM espelhar exatamente os usados no codigo.
Ver tests/test_enums_domain_states.py, que ancora cada valor na sua fonte real.
"""
from enum import StrEnum

__all__ = [
    "PaymentStatus",
    "WalkOperationalStatus",
    "ApplicationStatus",
    "TenantWalkerAccessStatus",
]


class PaymentStatus(StrEnum):
    """Estado interno do pagamento de um passeio.

    Fonte: app/routes/payments.py (STATUS_BY_ASAAS_STATUS / STATUS_BY_EVENT e
    as constantes _PAYMENT_CONFIRMED_STATUS / _PAYMENT_REFUNDED_STATUS).
    """

    PAGAMENTO_SANDBOX_CRIADO = "pagamento_sandbox_criado"
    AGUARDANDO_PAGAMENTO = "aguardando_pagamento"
    PAGAMENTO_CONFIRMADO_SANDBOX = "pagamento_confirmado_sandbox"
    FALHA_PAGAMENTO = "falha_pagamento"
    PAGAMENTO_ESTORNADO = "pagamento_estornado"


class WalkOperationalStatus(StrEnum):
    """Estado operacional do passeio (maquina de matching/execucao).

    Fonte: app/services/operational_matching_service.py (constantes de modulo)
    e o estado de espera de pagamento "awaiting_payment" setado em
    app/routes/walks.py / liberado em app/routes/payments.py.
    Default de Walk.operational_status = ``ride_scheduled``.
    """

    AWAITING_PAYMENT = "awaiting_payment"
    PENDING_WALKER_CONFIRMATION = "pending_walker_confirmation"
    WALKER_ACCEPTED = "walker_accepted"
    WALKER_DECLINED = "walker_declined"
    AUTO_REMATCHING = "auto_rematching"
    NO_WALKER_FOUND = "no_walker_found"
    RIDE_SCHEDULED = "ride_scheduled"
    WALKER_ARRIVING = "walker_arriving"
    RIDE_IN_PROGRESS = "ride_in_progress"
    RIDE_COMPLETED = "ride_completed"
    RIDE_CANCELLED = "ride_cancelled"
    AWAITING_TUTOR_RECONFIRMATION = "awaiting_tutor_reconfirmation"


class ApplicationStatus(StrEnum):
    """Estado da candidatura de credenciamento do passeador.

    Fonte: app/routes/walker.py (_canonical_application_status + labels) e
    WalkerProfile.status (app/models/walker_profile.py, default "pending" que
    e canonicalizado para "submitted").
    """

    SUBMITTED = "submitted"
    UNDER_REVIEW = "under_review"
    APPROVED = "approved"
    ACTIVE = "active"
    REJECTED = "rejected"
    RESUBMISSION_REQUESTED = "resubmission_requested"
    BLOCKED = "blocked"


class TenantWalkerAccessStatus(StrEnum):
    """Estado do convite/vinculo de um passeador a Rede Aumigao de um tenant.

    Fonte: app/models/tenant_walker_access.py (docstring da maquina de estados).
    ``paused`` e mantido por compatibilidade com dados legados.
    """

    PENDING = "pending"
    ACTIVE = "active"
    DECLINED = "declined"
    REVOKED = "revoked"
    PAUSED = "paused"
