"""Constantes de negócio para o sistema de CR (Créditos de Reputação) do passeador.

Estes valores foram aprovados pelo Carlos e são a fonte única de verdade para
as regras de ganho, penalidade, gasto e marcos de badge do sistema de CR.

A concessão de badges (marcos de passeios e de nível) é implementada nos ganchos
de Fase 4. Este módulo apenas expõe as constantes — NÃO contém lógica de concessão.
"""
from __future__ import annotations

# ── Ganhos de CR ────────────────────────────────────────────────────────────
# Mapeado por source → amount creditado na carteira.
CR_EARN: dict[str, int] = {
    "walk_completed": 10,
    "review_5star": 5,
    "weekly_mission": 20,
    "kit_approved": 15,
}

# ── Penalidades de CR ────────────────────────────────────────────────────────
# Mapeado por source → amount SUBTRAÍDO (valor positivo = quanto se perde).
CR_PENALTY: dict[str, int] = {
    "no_show": 15,
}

# ── Gastos de CR (boosts) ────────────────────────────────────────────────────
# Mapeado por source → custo a debitar.
CR_SPEND: dict[str, int] = {
    "boost_24h": 50,
}

# ── Marcos de badge por volume de passeios ──────────────────────────────────
# Quando o walker atinge estes totais de passeios concluídos, recebe um badge.
# Concessão real é feita pelos ganchos de Fase 4.
BADGE_WALK_MILESTONES: list[int] = [50, 100, 500]

# ── Níveis que viram badge ───────────────────────────────────────────────────
# Ao subir de nível, o walker recebe um badge correspondente.
# Concessão real é feita pelos ganchos de Fase 4.
BADGE_LEVELS: list[str] = ["Bronze", "Prata", "Ouro"]
