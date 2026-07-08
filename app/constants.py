"""Constantes de domínio compartilhadas entre módulos.

Este módulo é NEUTRO: não importa nada do próprio projeto, eliminando
qualquer risco de import circular. Importe daqui, nunca duplique.
"""

# ---------------------------------------------------------------------------
# Status de pagamento considerados "pagos / confirmados"
# ---------------------------------------------------------------------------
# Estes valores refletem o que o gateway (Asaas/Efí) grava em Payment.status.
# Sincronize aqui se novos aliases de gateway forem adicionados.
PAID_PAYMENT_STATUSES: frozenset[str] = frozenset({
    "paid",
    "Pago",
    "pagamento_confirmado_sandbox",
    "payment_confirmed",
    "confirmed",
})

# ---------------------------------------------------------------------------
# Cortes de nível do passeador (Bronze→Diamante) — fonte única de verdade (B3)
# ---------------------------------------------------------------------------
# Estes valores são referenciados por walker_trust_service.compute_walker_level
# (função oficial), por reputation_service.walker_level (versão simples/bulk)
# e por _walker_level em walker.py (dashboard/UI).
# Altere AQUI para ajustar os limiares em todo o sistema.
LEVEL_PRATA_MIN_WALKS: int = 10
LEVEL_PRATA_MIN_RATING: float = 4.5
LEVEL_OURO_MIN_WALKS: int = 50
LEVEL_OURO_MIN_RATING: float = 4.7
LEVEL_DIAMANTE_MIN_WALKS: int = 150
LEVEL_DIAMANTE_MIN_RATING: float = 4.9

# ---------------------------------------------------------------------------
# Status de passeio considerados "concluídos" (conjunto canônico amplo)
# ---------------------------------------------------------------------------
# Decisão do dono (B2, 2026-06-21): ride_completed E variantes acentuadas
# contam como concluído em TODO lugar — reputação, nível, receita e admin.
#
# Uso:
#   - Contagem de passeios concluídos para reputação/nível (reputation_service)
#   - Listagens e filtros de "concluídos" (walks, admin, walker routes)
#   - Guardas de controle de fluxo que bloqueiam re-finalização direta
#     (qualquer status deste conjunto não pode ser setado manualmente pelo
#     tutor/walker/admin; a conclusão deve passar pela revisão operacional)
#
# Não usar para filtros que precisem distinguir "em progresso" de "concluído":
# nesse caso, use WalkOperationalStatus diretamente.
WALK_COMPLETED_STATUSES: frozenset[str] = frozenset({
    "Finalizado",
    "Concluido",
    "Concluído",
    "finalizado",
    "completed",
    "finished",
    "ride_completed",
})

# ---------------------------------------------------------------------------
# Itens do "kit" do passeador (agua, vasilha, saquinhos etc.) — fonte única (T2)
# ---------------------------------------------------------------------------
# Movido de routes/walker.py para cá: era duplicado seria necessário para expor
# o kit aprovado no payload de matching (services/matching_service.py), que não
# pode importar de routes/walker.py (ciclo: walker.py -> operational_matching_
# service.py -> matching_service.py -> walker.py). Referenciado também por
# routes/admin.py (aprovação do kit).
KIT_ITEM_DEFINITIONS: list[dict] = [
    {"key": "water", "label": "Agua", "description": "Garrafa lacrada ou propria para hidratacao."},
    {"key": "bowl", "label": "Vasilha para agua", "description": "Vasilha ou pote portatil para oferecer agua."},
    {"key": "bags", "label": "Saquinho para necessidades", "description": "Saquinhos higienicos suficientes para o passeio."},
    {"key": "first_aid", "label": "Primeiros socorros", "description": "Kit simples para pequenas ocorrencias."},
    {"key": "towel", "label": "Toalha/pano", "description": "Pano limpo para secar patas ou pequenas sujeiras."},
    {"key": "premium_treats", "label": "Itens premium", "description": "Petiscos autorizados e outros itens de conforto."},
]
