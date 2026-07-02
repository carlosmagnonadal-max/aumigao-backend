"""Plano `free` ("Começar") + reverse trial — regras centrais.

Decisão do Carlos (matriz final, docs/go-to-market/03-plano-gratuito-freemium.md):
  Plano `free`: R$0/mês · comissão PRÓPRIA 20% · REDE desligada · sem multiplicadores
  · cap de 40 passeios próprios/mês · SÓ passeio avulso individual (nada de shared
  walks / pet tour / recorrência).
  Reverse trial: tenant novo entra como `free` MAS roda como Pro completo por 21 dias
  (comissão Pro + rede + multiplicadores) → depois é rebaixado para free de fato.

Nenhuma dimensão do free pode ser melhor que a do Pro (escada monotônica):
  mensalidade 0 → 129,90 · comissão própria 20 → 10 · rede off → 18.

Princípio de implementação: TUDO que decide capability por plano deve consultar o
PLANO EFETIVO (`effective_tenant_plan`), que devolve "pro" durante o trial ativo e o
plano real caso contrário. Assim o trial libera rede + multiplicadores + comissão Pro
sem espalhar lógica de trial por vários módulos.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

# BRT fixo (UTC-3): o Brasil aboliu o horário de verão em 2019 — mesmo padrão do
# faturamento mensal de comissão (payments.py). Evita depender de tzdata no container.
_BRT = timezone(timedelta(hours=-3))

# Chave canônica do plano gratuito.
TENANT_PLAN_FREE = "free"

# Comissão própria do plano free (take-rate próprio). 20% por decisão do Carlos.
FREE_PLAN_COMMISSION_PERCENT = 20.0

# Duração padrão do reverse trial (em dias) — tenant novo roda como Pro por N dias.
FREE_PLAN_TRIAL_DAYS = 21

# Cap default de passeios PRÓPRIOS por mês no plano free. Configurável via env.
_DEFAULT_WALK_CAP = 40


def free_plan_walk_cap() -> int:
    """Cap mensal de passeios próprios do plano free (env FREE_PLAN_WALK_CAP, default 40).

    Valor inválido/não-positivo cai no default (não desliga o cap por engano de config).
    """
    raw = os.getenv("FREE_PLAN_WALK_CAP", str(_DEFAULT_WALK_CAP))
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return _DEFAULT_WALK_CAP
    return value if value > 0 else _DEFAULT_WALK_CAP


def is_free_plan(plan: str | None) -> bool:
    """True se a chave (real) do plano é `free`."""
    return (plan or "").strip().lower() == TENANT_PLAN_FREE


def trial_is_active(tenant, *, now: datetime | None = None) -> bool:
    """True se o tenant está dentro do reverse trial (trial_ends_at no futuro).

    Só faz sentido para tenants no plano `free`; um tenant pro/enterprise nunca tem
    trial_ends_at preenchido, então retorna False naturalmente.
    """
    ends_at = getattr(tenant, "trial_ends_at", None)
    if not ends_at:
        return False
    reference = now or datetime.utcnow()
    return ends_at > reference


def effective_tenant_plan(tenant, *, now: datetime | None = None) -> str:
    """Plano EFETIVO para resolução de capabilities.

    - Tenant `free` COM trial ativo → "pro" (comissão Pro + rede + multiplicadores).
    - Qualquer outro caso → plano real do tenant.

    Nenhum efeito para tenants pro/enterprise (não têm trial) → zero-regressão.
    """
    plan = (getattr(tenant, "plan", None) or "").strip().lower()
    if plan == TENANT_PLAN_FREE and trial_is_active(tenant, now=now):
        return "pro"
    return plan


def compute_trial_ends_at(created_at: datetime | None = None) -> datetime:
    """Fim do reverse trial = criação + FREE_PLAN_TRIAL_DAYS dias."""
    base = created_at or datetime.utcnow()
    return base + timedelta(days=FREE_PLAN_TRIAL_DAYS)


# ── Cap mensal de passeios (plano free) ─────────────────────────────────────

def current_month_window_utc(now: datetime | None = None) -> tuple[datetime, str]:
    """Início do mês corrente em BRT, convertido para UTC NAIVE + rótulo 'YYYY-MM'.

    Walk.created_at é armazenado naive-UTC (datetime.utcnow), então o corte do
    mês BRT precisa virar um bound naive-UTC para a query.
    """
    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    now_brt = reference.astimezone(_BRT)
    month_start_brt = now_brt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    start_utc_naive = month_start_brt.astimezone(timezone.utc).replace(tzinfo=None)
    return start_utc_naive, f"{now_brt.year:04d}-{now_brt.month:02d}"


def count_tenant_walks_current_month(db, tenant_id: str, *, now: datetime | None = None) -> int:
    """Passeios do tenant CRIADOS no mês corrente (BRT), excluindo cancelados.

    Regra de contagem (decisão documentada nos testes):
      - conta por created_at (criação/agendamento é a fronteira anti-abuso);
      - EXCLUI status 'cancelado' (case-insensitive — valor canônico 'Cancelado'):
        passeio cancelado devolve a vaga do cap;
      - inclui aguardando_pagamento/agendado/concluído (criados não-cancelados).
    """
    from sqlalchemy import func

    from app.models.walk import Walk

    start_utc, _ = current_month_window_utc(now)
    return (
        db.query(func.count(Walk.id))
        .filter(
            Walk.tenant_id == tenant_id,
            Walk.created_at >= start_utc,
            func.lower(func.coalesce(Walk.status, "")) != "cancelado",
        )
        .scalar()
        or 0
    )


def enforce_free_plan_walk_cap(db, tenant, *, now: datetime | None = None) -> None:
    """Trava de criação de passeio no plano free: cap mensal (default 40).

    Só se aplica ao plano EFETIVO free (reverse trial em curso → sem cap, é Pro).
    Ao atingir o cap → 403 com mensagem clara de upgrade. Pro/enterprise: no-op.
    """
    from fastapi import HTTPException

    if tenant is None or not is_free_plan(effective_tenant_plan(tenant, now=now)):
        return
    cap = free_plan_walk_cap()
    used = count_tenant_walks_current_month(db, tenant.id, now=now)
    if used >= cap:
        raise HTTPException(
            status_code=403,
            detail=(
                f"Limite de {cap} passeios/mês do plano gratuito atingido "
                f"({used}/{cap}). Faça upgrade para o plano Pro e tenha passeios "
                "ilimitados, rede de passeadores e recorrência."
            ),
        )
