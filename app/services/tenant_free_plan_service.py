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

# Máximo de pets por TUTOR no plano free. Configurável via env.
_DEFAULT_PETS_PER_TUTOR = 2

# ── Features BLOQUEADAS por PLANO no free (independente dos toggles por tenant) ──
# Multiplicadores de receita + "Evolução do Pet" pro-only (mapa do Carlos 2026-07-02).
# O bloqueio é aplicado NOS CHOKE POINTS de tenant_plan_service (tenant_feature_enabled/
# tenant_has_feature), POR CIMA dos gates existentes (3 camadas do pet_live_profile,
# TenantFeature etc.) — sem tocá-los. Trial 21d libera tudo (plano efetivo = pro).
#
# LIBERADO no free (NÃO listar aqui): pet_live_profile (cadastro/ficha do pet),
# walk_observations_form (observação do passeador no relatório do passeio),
# background_checks (captação de confiança), tips, reviews, live_gps,
# push_notifications, protected_chat, weekly_missions, tutor_gamification etc.
FREE_PLAN_BLOCKED_FEATURE_KEYS: frozenset[str] = frozenset({
    # Multiplicadores de receita
    "recurring_plans",     # planos recorrentes / créditos
    "coupons",             # cupons de desconto
    "client_referrals",    # referral do tutor
    "walker_referrals",    # referral do passeador
    "walker_boosts",       # boosts de passeador
    "shared_walks",        # passeios compartilhados (modalidade Pro+)
    "pet_tour",            # Pet Tour (modalidade Pro+)
    # Evolução do Pet — pro-only por chave inteira
    "pet_alerts",          # alertas/lembretes (sweep não gera pra tenant free)
    "pet_share",           # share público do perfil do pet
})


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


def plan_blocks_feature(tenant, key: str, *, now: datetime | None = None) -> bool:
    """True se o PLANO EFETIVO do tenant bloqueia a feature (só acontece no free).

    Free em trial ativo → plano efetivo "pro" → nada bloqueado.
    Pro/enterprise → nunca bloqueiam aqui → zero-regressão.
    """
    normalized = (key or "").strip()
    if normalized not in FREE_PLAN_BLOCKED_FEATURE_KEYS:
        return False
    return is_free_plan(effective_tenant_plan(tenant, now=now))


def free_plan_upgrade_exception(feature: str, label: str):
    """403 com shape de TEASER documentado (contrato pro admin-web/app).

    Body: {"detail": {"code": "plan_upgrade_required", "required_plan": "pro",
                       "feature": "<chave>", "message": "<label> disponível a
                       partir do plano Pro."}}
    O cliente usa `code` para renderizar o CTA de upgrade em vez de erro genérico.
    """
    from fastapi import HTTPException

    return HTTPException(
        status_code=403,
        detail={
            "code": "plan_upgrade_required",
            "required_plan": "pro",
            "feature": feature,
            "message": f"{label} disponível a partir do plano Pro.",
        },
    )


def free_plan_pets_per_tutor() -> int:
    """Máximo de pets por tutor no plano free (env FREE_PLAN_PETS_PER_TUTOR, default 2).

    Valor inválido/não-positivo cai no default (não desliga o limite por engano).
    """
    raw = os.getenv("FREE_PLAN_PETS_PER_TUTOR", str(_DEFAULT_PETS_PER_TUTOR))
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return _DEFAULT_PETS_PER_TUTOR
    return value if value > 0 else _DEFAULT_PETS_PER_TUTOR


def enforce_free_plan_pet_limit(db, tenant, tutor_id: str) -> None:
    """Trava de criação de pet NOVO no plano free: máx N pets por tutor (default 2).

    Só bloqueia CRIAÇÃO — downgrade do trial NÃO remove pets excedentes (o tutor
    mantém os que já tem; apenas não cria novos acima do limite). Trial 21d isento
    (plano efetivo = pro). Pro/enterprise: no-op.
    """
    from fastapi import HTTPException

    if tenant is None or not is_free_plan(effective_tenant_plan(tenant)):
        return
    from app.models.pet import Pet

    limit = free_plan_pets_per_tutor()
    current = db.query(Pet).filter(Pet.tutor_id == tutor_id).count()
    if current >= limit:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "plan_upgrade_required",
                "required_plan": "pro",
                "feature": "pets_per_tutor",
                "message": (
                    f"Limite de {limit} pets por tutor no plano gratuito atingido. "
                    "Faça upgrade para o plano Pro para cadastrar mais pets."
                ),
            },
        )


def enforce_pet_evolution_allowed(tenant, *, feature: str, label: str) -> None:
    """Gate por PLANO das rotas pro-only da Evolução do Pet (timeline/stats).

    Aplicado POR CIMA dos gates 3-camadas existentes (que continuam decidindo 404
    quando a feature está dormente). A chave `pet_live_profile` NÃO é bloqueada por
    chave inteira porque o cadastro/ficha do pet (PATCH profile) fica LIBERADO no
    free — só timeline/histórico/stats são pro-only, daí o gate por rota.
    """
    if tenant is not None and is_free_plan(effective_tenant_plan(tenant)):
        raise free_plan_upgrade_exception(feature, label)


# ── Nudge de upgrade: uso do plano (contrato pro admin-web) ─────────────────

# Mensalidade do Pro para a projeção do nudge (fonte: tenant_saas_pricing).
_PRO_MONTHLY_FEE = 129.90
_PRO_COMMISSION_PERCENT = 10.0


def build_plan_usage(db, tenant, *, now: datetime | None = None) -> dict:
    """Payload do nudge de upgrade (GET /admin/tenants/{id}/plan-usage).

    SHAPE JSON (contrato pro admin-web — manter estável):
    {
      "tenant_id": str,
      "plan": str,                    # plano REAL ("free"/"pro"/"enterprise"/legado)
      "effective_plan": str,          # plano efetivo (trial-aware)
      "trial": {
        "active": bool,
        "ends_at": str|null,          # ISO-8601
        "days_left": int|null,        # dias restantes (>=0); null sem trial
        "downgraded_at": str|null     # ISO-8601 do carimbo de downgrade
      },
      "period": "YYYY-MM",            # mês corrente em BRT
      "walks": {
        "used": int,                  # criados não-cancelados no mês (BRT)
        "cap": int|null,              # null = ilimitado (pro/enterprise/trial)
        "remaining": int|null
      },
      "limits": {"pets_per_tutor": int|null},   # null = sem limite
      "commission": {
        "percent": float,             # % vigente (trial-aware, respeita custom)
        "month_total": float,         # comissão medida no mês (R$)
        "gmv_month": float            # GMV medido no mês (R$, base da comissão)
      },
      "pro_projection": {             # quanto custaria no Pro este mês
        "monthly_fee": 129.90,
        "commission_percent": 10.0,
        "commission_month": float,    # 10% × gmv_month
        "total_month": float,         # 129.90 + commission_month
        "savings_month": float        # comissão atual − total Pro (>0 = Pro compensa)
      },
      "upgrade_recommended": bool     # savings>0 OU cap atingido (só p/ free)
    }
    """
    from sqlalchemy import func as _func

    from app.core.money import q2, to_float, to_money
    from app.models.commission_entry import COMM_VOID, CommissionEntry
    from app.services.payment_split_service import get_commission_percent

    reference = now or datetime.now(timezone.utc)
    _, period = current_month_window_utc(reference)
    plan = (getattr(tenant, "plan", None) or "").strip().lower()
    eff_plan = effective_tenant_plan(tenant, now=reference.replace(tzinfo=None))
    on_free = is_free_plan(eff_plan)

    # Trial
    ends_at = getattr(tenant, "trial_ends_at", None)
    trial_active = trial_is_active(tenant, now=reference.replace(tzinfo=None))
    days_left = None
    if ends_at is not None:
        remaining_s = (ends_at - reference.replace(tzinfo=None)).total_seconds()
        days_left = max(0, int(remaining_s // 86400))
    downgraded_at = getattr(tenant, "trial_downgraded_at", None)

    # Passeios do mês (mesma regra do cap: criados não-cancelados, BRT)
    used = count_tenant_walks_current_month(db, tenant.id, now=reference)
    cap = free_plan_walk_cap() if on_free else None
    remaining = max(0, cap - used) if cap is not None else None

    # Comissão medida no mês (Decimal; exclui entradas VOID)
    rows = (
        db.query(
            _func.coalesce(_func.sum(CommissionEntry.amount), 0),
            _func.coalesce(_func.sum(CommissionEntry.walk_price), 0),
        )
        .filter(
            CommissionEntry.tenant_id == tenant.id,
            CommissionEntry.period == period,
            CommissionEntry.status != COMM_VOID,
        )
        .first()
    )
    month_total = q2(to_money(rows[0] if rows else 0))
    gmv_month = q2(to_money(rows[1] if rows else 0))
    commission_percent = get_commission_percent(db, tenant.id)

    # Projeção Pro: 129,90 + 10% × GMV (Decimal até a borda)
    pro_commission = q2(gmv_month * to_money(_PRO_COMMISSION_PERCENT) / to_money(100))
    pro_total = q2(to_money(_PRO_MONTHLY_FEE) + pro_commission)
    savings = q2(month_total - pro_total)

    return {
        "tenant_id": tenant.id,
        "plan": plan,
        "effective_plan": eff_plan,
        "trial": {
            "active": trial_active,
            "ends_at": ends_at.isoformat() if ends_at else None,
            "days_left": days_left,
            "downgraded_at": downgraded_at.isoformat() if downgraded_at else None,
        },
        "period": period,
        "walks": {"used": used, "cap": cap, "remaining": remaining},
        "limits": {"pets_per_tutor": free_plan_pets_per_tutor() if on_free else None},
        "commission": {
            "percent": commission_percent,
            "month_total": to_float(month_total),
            "gmv_month": to_float(gmv_month),
        },
        "pro_projection": {
            "monthly_fee": _PRO_MONTHLY_FEE,
            "commission_percent": _PRO_COMMISSION_PERCENT,
            "commission_month": to_float(pro_commission),
            "total_month": to_float(pro_total),
            "savings_month": to_float(savings),
        },
        "upgrade_recommended": bool(
            on_free and (savings > 0 or (cap is not None and used >= cap))
        ),
    }


# ── Reverse trial: downgrade lazy + notificação ─────────────────────────────

def maybe_downgrade_expired_trial(db, tenant, *, now: datetime | None = None) -> bool:
    """Carimba (idempotente) o downgrade do reverse trial expirado e notifica o admin.

    IMPORTANTE — dinheiro NÃO depende deste carimbo: toda a resolução econômica
    (comissão, rede, features, cap) é STATELESS via effective_tenant_plan; expirar
    o trial já muda o comportamento no request seguinte. Este carimbo só faz o
    bookkeeping (trial_downgraded_at) + garante a config em 20% + notificação de
    loss-aversion aos admins do tenant, UMA única vez.

    Não faz commit — o caller comita. Retorna True se carimbou agora.
    """
    if tenant is None or not is_free_plan(getattr(tenant, "plan", None)):
        return False
    ends_at = getattr(tenant, "trial_ends_at", None)
    if not ends_at or trial_is_active(tenant, now=now):
        return False
    if getattr(tenant, "trial_downgraded_at", None) is not None:
        return False  # já carimbado — notificação única

    reference = now or datetime.utcnow()
    tenant.trial_downgraded_at = reference
    db.add(tenant)

    # Garante a comissão do free (20%) na config — normalmente já está (a config
    # nasce com o default do plano); só corrige drift, respeitando override manual.
    try:
        from app.services.payment_split_service import get_or_create_payment_config

        cfg = get_or_create_payment_config(db, tenant.id)
        if not cfg.commission_is_custom and float(cfg.commission_percent or 0) != FREE_PLAN_COMMISSION_PERCENT:
            cfg.commission_percent = FREE_PLAN_COMMISSION_PERCENT
    except Exception:  # noqa: BLE001 — bookkeeping best-effort, nunca quebra o request
        pass

    # Notifica os admins do tenant (padrão de notificação existente).
    try:
        from app.models.user import User
        from app.routes.notifications import NotificationCreate, _create_notification

        admins = (
            db.query(User)
            .filter(User.tenant_id == tenant.id, User.role == "admin", User.is_active.is_(True))
            .all()
        )
        for admin in admins:
            _create_notification(
                db,
                NotificationCreate(
                    tenant_id=tenant.id,
                    user_id=admin.id,
                    user_role="admin",
                    title="Seu período de teste do plano Pro terminou",
                    message=(
                        "Sua conta voltou ao plano gratuito. Rede de passeadores, "
                        "planos recorrentes, cupons, boosts e a Evolução do Pet "
                        "ficam disponíveis a partir do plano Pro (R$ 129,90/mês)."
                    ),
                    type="warning",
                    related_entity_type="tenant",
                    related_entity_id=tenant.id,
                    metadata={"event": "free_trial_downgrade", "required_plan": "pro"},
                ),
            )
    except Exception:  # noqa: BLE001 — notificação best-effort, nunca quebra o request
        pass

    return True


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
            detail={
                "code": "plan_upgrade_required",
                "required_plan": "pro",
                "feature": "walk_cap",
                "message": (
                    f"Limite de {cap} passeios/mês do plano gratuito atingido "
                    f"({used}/{cap}). Faça upgrade para o plano Pro e tenha passeios "
                    "ilimitados, rede de passeadores e recorrência."
                ),
            },
        )
