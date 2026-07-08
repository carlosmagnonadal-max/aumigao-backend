"""Testes da economia do passeio de plano (decisão 07/07/2026).

Regra de ouro: passeador intocado em reais (residual da âncora cheia);
desconto co-financiado pro-rata plataforma:tenant; piso dinâmico
min(comissão+margem, take de rede).
"""
import pytest

from app.services.plan_walk_economics import (
    compute_plan_walk_split,
    max_plan_discount_percent,
)

# Cenário canônico Aumigão pós-âncora: avulso 54,90 · comissão 10% · margem 8,2%.
ANCHOR = 54.90
C = 10.0
M = 8.2


def test_walker_untouched_at_anchor():
    """Passeador recebe o MESMO residual do avulso, qualquer que seja o desconto."""
    avulso = compute_plan_walk_split(ANCHOR, ANCHOR, C, M)
    plano15 = compute_plan_walk_split(ANCHOR * 0.85, ANCHOR, C, M)
    plano18 = compute_plan_walk_split(ANCHOR * 0.82, ANCHOR, C, M)
    assert avulso["walker_amount"] == plano15["walker_amount"] == plano18["walker_amount"]
    # 54,90 × (100 − 18,2)% = 44,91 — o repasse histórico, em reais.
    assert plano15["walker_amount"] == pytest.approx(44.91, abs=0.01)


def test_split_invariant_sums_to_effective():
    """platform + tenant + walker == valor efetivo do plano, exatamente."""
    effective = round(ANCHOR * 0.85, 2)
    split = compute_plan_walk_split(effective, ANCHOR, C, M)
    total = round(split["platform_amount"] + split["tenant_amount"] + split["walker_amount"], 2)
    assert total == pytest.approx(effective, abs=0.001)


def test_remaining_split_pro_rata():
    """Sobra (efetivo − passeador) dividida na proporção comissão:margem."""
    effective = round(ANCHOR * 0.85, 2)  # 46.67 (arredondado a centavo)
    split = compute_plan_walk_split(effective, ANCHOR, C, M)
    remaining = effective - 44.91
    assert split["platform_amount"] == pytest.approx(remaining * C / (C + M), abs=0.01)
    assert split["tenant_amount"] == pytest.approx(remaining * M / (C + M), abs=0.01)
    assert split["platform_amount"] > 0
    assert split["tenant_amount"] > 0


def test_no_discount_equals_regular_split():
    """Plano sem desconto (efetivo = âncora) reproduz o split do avulso."""
    split = compute_plan_walk_split(ANCHOR, ANCHOR, C, M)
    assert split["walker_amount"] == pytest.approx(44.91, abs=0.01)
    assert split["platform_amount"] == pytest.approx(5.49, abs=0.01)
    assert split["tenant_amount"] == pytest.approx(4.50, abs=0.01)


def test_legacy_deficit_plan_walker_still_untouched():
    """Plano legado abaixo do piso: walker intocado, déficit visível no tenant,
    plataforma nunca negativa."""
    effective = 40.00  # abaixo do repasse de 44,91
    split = compute_plan_walk_split(effective, ANCHOR, C, M)
    assert split["walker_amount"] == pytest.approx(44.91, abs=0.01)
    assert split["platform_amount"] == 0.0
    assert split["tenant_amount"] == pytest.approx(40.00 - 44.91, abs=0.01)  # negativo, honesto


def test_zero_slices_remaining_goes_to_platform():
    """c=m=0 (config zerada): sobra vai pra plataforma, sem divisão por zero."""
    split = compute_plan_walk_split(50.0, 50.0, 0.0, 0.0)
    assert split["walker_amount"] == pytest.approx(50.0)
    assert split["platform_amount"] == 0.0
    assert split["tenant_amount"] == 0.0


def test_max_discount_is_min_of_own_and_network():
    """Piso dinâmico: min(comissão+margem, rede+margem) = margem + min(c, rede)."""
    # Ambos os ramos co-financiados pela margem: 8,2 + min(10, 18) = 18,2.
    assert max_plan_discount_percent(10.0, 8.2, 18.0) == pytest.approx(18.2)
    # Margem gorda: 25 + min(10, 18) = 35.
    assert max_plan_discount_percent(10.0, 25.0, 18.0) == pytest.approx(35.0)
    # Enterprise REAL do aumigao (comissão 5, rede 10, margem 25): 25 + 5 = 30.
    assert max_plan_discount_percent(5.0, 25.0, 10.0) == pytest.approx(30.0)
    # Margem 3, comissão 5, rede 10: 3 + 5 = 8 (fatia própria limita).
    assert max_plan_discount_percent(5.0, 3.0, 10.0) == pytest.approx(8.0)
    # Nada configurado: 0.
    assert max_plan_discount_percent(0.0, 0.0, 18.0) == 0.0
    # Rede indisponível (take 0, ex. Free): só o ramo próprio limita — 20+20=40.
    assert max_plan_discount_percent(20.0, 20.0, 0.0) == pytest.approx(40.0)


def test_network_branch_platform_absorbs_discount():
    """Ramo rede (comissão 18, margem do tenant m): passeador pela âncora,
    tenant recebe a fatia dele da sobra, plataforma absorve o desconto."""
    effective = round(ANCHOR * 0.85, 2)
    split = compute_plan_walk_split(effective, ANCHOR, 18.0, M)
    # Walker da rede: âncora × (100 − 18 − 8,2)% = 40,52.
    assert split["walker_amount"] == pytest.approx(ANCHOR * (100 - 18 - M) / 100, abs=0.01)
    total = split["platform_amount"] + split["tenant_amount"] + split["walker_amount"]
    assert total == pytest.approx(effective, abs=0.01)
    assert split["platform_amount"] > 0  # com desconto 15% ≤ piso 18, ninguém negativa
    assert split["tenant_amount"] > 0
