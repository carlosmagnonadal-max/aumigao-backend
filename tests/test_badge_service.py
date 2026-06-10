"""Testes de unidade para app/services/badge_service.py

Funcoes puras (sem DB). Testamos o COMPORTAMENTO REAL atual.
"""

from app.services.badge_service import generate_badges, generate_display_reason


# ---------------------------------------------------------------------------
# generate_badges
# ---------------------------------------------------------------------------


def test_sem_badges_quando_nada_se_aplica():
    """Item neutro: rank > 1, sem metricas relevantes.

    Atencao: para nao disparar "Novo no Aumigao" (total_walks < 5 e
    reviews_count <= 2), garantimos reviews_count alto.
    """
    item = {
        "rating_average": 0,
        "reviews_count": 3,
        "total_walks": 5,
        "proximity_score": 0,
        "final_matching_score": 0,
        "behavior_details": {},
    }
    ctx = {"best_rating": 5, "most_walks": 100}
    assert generate_badges(item, rank=2, ranking_context=ctx) == []


def test_rank_1_mais_recomendado():
    item = {
        "rating_average": 0,
        "reviews_count": 3,
        "total_walks": 5,
        "proximity_score": 0,
        "final_matching_score": 0,
        "behavior_details": {},
    }
    ctx = {"best_rating": 5, "most_walks": 100}
    assert generate_badges(item, rank=1, ranking_context=ctx) == ["Mais recomendado"]


def test_rank_diferente_de_1_nao_recebe_mais_recomendado():
    item = {
        "rating_average": 0,
        "reviews_count": 3,
        "total_walks": 5,
        "proximity_score": 0,
        "final_matching_score": 0,
        "behavior_details": {},
    }
    ctx = {"best_rating": 5, "most_walks": 100}
    assert "Mais recomendado" not in generate_badges(item, rank=5, ranking_context=ctx)


def test_melhor_avaliacao_requer_rating_e_minimo_5_reviews():
    # rating igual ao best_rating e exatamente 5 reviews -> dispara
    item = {
        "rating_average": 4.8,
        "reviews_count": 5,
        "total_walks": 0,
        "proximity_score": 0,
        "final_matching_score": 0,
        "behavior_details": {},
    }
    ctx = {"best_rating": 4.8, "most_walks": 100}
    badges = generate_badges(item, rank=2, ranking_context=ctx)
    assert "Melhor avaliacao" in badges


def test_melhor_avaliacao_nao_dispara_com_poucas_reviews():
    # rating ok mas apenas 4 reviews (< 5) -> nao dispara
    item = {
        "rating_average": 4.8,
        "reviews_count": 4,
        "total_walks": 0,
        "proximity_score": 0,
        "final_matching_score": 0,
        "behavior_details": {},
    }
    ctx = {"best_rating": 4.8, "most_walks": 100}
    badges = generate_badges(item, rank=2, ranking_context=ctx)
    assert "Melhor avaliacao" not in badges


def test_melhor_avaliacao_rating_acima_do_best_tambem_dispara():
    # rating_average >= best_rating: usar maior que tambem vale
    item = {
        "rating_average": 5.0,
        "reviews_count": 10,
        "total_walks": 0,
        "proximity_score": 0,
        "final_matching_score": 0,
        "behavior_details": {},
    }
    ctx = {"best_rating": 4.5, "most_walks": 100}
    assert "Melhor avaliacao" in generate_badges(item, rank=2, ranking_context=ctx)


def test_mais_experiente_requer_most_walks_e_minimo_10():
    item = {
        "rating_average": 0,
        "reviews_count": 3,
        "total_walks": 10,
        "proximity_score": 0,
        "final_matching_score": 0,
        "behavior_details": {},
    }
    ctx = {"best_rating": 5, "most_walks": 10}
    assert "Mais experiente" in generate_badges(item, rank=2, ranking_context=ctx)


def test_mais_experiente_nao_dispara_abaixo_de_10_walks():
    # total_walks 9: atinge most_walks mas nao o piso de 10
    item = {
        "rating_average": 0,
        "reviews_count": 3,
        "total_walks": 9,
        "proximity_score": 0,
        "final_matching_score": 0,
        "behavior_details": {},
    }
    ctx = {"best_rating": 5, "most_walks": 9}
    assert "Mais experiente" not in generate_badges(item, rank=2, ranking_context=ctx)


def test_mais_experiente_nao_dispara_abaixo_de_most_walks():
    # total_walks 12 >= 10 mas < most_walks (20)
    item = {
        "rating_average": 0,
        "reviews_count": 3,
        "total_walks": 12,
        "proximity_score": 0,
        "final_matching_score": 0,
        "behavior_details": {},
    }
    ctx = {"best_rating": 5, "most_walks": 20}
    assert "Mais experiente" not in generate_badges(item, rank=2, ranking_context=ctx)


def test_responde_rapido_threshold_80():
    item = {
        "rating_average": 0,
        "reviews_count": 3,
        "total_walks": 5,
        "proximity_score": 0,
        "final_matching_score": 0,
        "behavior_details": {"response_time_score": 80},
    }
    ctx = {"best_rating": 5, "most_walks": 100}
    assert "Responde rapido" in generate_badges(item, rank=2, ranking_context=ctx)


def test_responde_rapido_nao_dispara_abaixo_de_80():
    item = {
        "rating_average": 0,
        "reviews_count": 3,
        "total_walks": 5,
        "proximity_score": 0,
        "final_matching_score": 0,
        "behavior_details": {"response_time_score": 79},
    }
    ctx = {"best_rating": 5, "most_walks": 100}
    assert "Responde rapido" not in generate_badges(item, rank=2, ranking_context=ctx)


def test_perto_de_voce_threshold_85():
    item = {
        "rating_average": 0,
        "reviews_count": 3,
        "total_walks": 5,
        "proximity_score": 85,
        "final_matching_score": 0,
        "behavior_details": {},
    }
    ctx = {"best_rating": 5, "most_walks": 100}
    assert "Perto de voce" in generate_badges(item, rank=2, ranking_context=ctx)


def test_destaque_da_regiao_requer_proximity_70_e_matching_75():
    # proximity 70 (>=70) mas < 85 (nao pega "Perto de voce"), matching 75
    item = {
        "rating_average": 0,
        "reviews_count": 3,
        "total_walks": 5,
        "proximity_score": 70,
        "final_matching_score": 75,
        "behavior_details": {},
    }
    ctx = {"best_rating": 5, "most_walks": 100}
    badges = generate_badges(item, rank=2, ranking_context=ctx)
    assert "Destaque da regiao" in badges
    assert "Perto de voce" not in badges


def test_destaque_da_regiao_nao_dispara_sem_matching():
    item = {
        "rating_average": 0,
        "reviews_count": 3,
        "total_walks": 5,
        "proximity_score": 72,
        "final_matching_score": 74,
        "behavior_details": {},
    }
    ctx = {"best_rating": 5, "most_walks": 100}
    assert "Destaque da regiao" not in generate_badges(item, rank=2, ranking_context=ctx)


def test_novo_no_aumigao_poucos_walks_e_poucas_reviews():
    # total_walks < 5 e reviews_count <= 2
    item = {
        "rating_average": 0,
        "reviews_count": 2,
        "total_walks": 4,
        "proximity_score": 0,
        "final_matching_score": 0,
        "behavior_details": {},
    }
    ctx = {"best_rating": 5, "most_walks": 100}
    assert "Novo no Aumigao" in generate_badges(item, rank=2, ranking_context=ctx)


def test_novo_no_aumigao_nao_dispara_com_3_reviews():
    item = {
        "rating_average": 0,
        "reviews_count": 3,
        "total_walks": 4,
        "proximity_score": 0,
        "final_matching_score": 0,
        "behavior_details": {},
    }
    ctx = {"best_rating": 5, "most_walks": 100}
    assert "Novo no Aumigao" not in generate_badges(item, rank=2, ranking_context=ctx)


def test_defaults_quando_chaves_ausentes():
    """item e ranking_context vazios. Com defaults:
    rating_average=0 >= best_rating=0 -> verdadeiro, mas reviews_count=0 < 5 -> nao.
    total_walks=0 >= most_walks=0 -> verdadeiro, mas < 10 -> nao.
    total_walks=0 < 5 e reviews_count=0 <= 2 -> 'Novo no Aumigao'.
    """
    badges = generate_badges({}, rank=2, ranking_context={})
    assert badges == ["Novo no Aumigao"]


def test_maximo_de_3_badges():
    """Item que dispara mais de 3 condicoes; retorna apenas as 3 primeiras
    na ordem de avaliacao do codigo:
    1) Mais recomendado (rank 1)
    2) Melhor avaliacao (rating>=best e reviews>=5)
    3) Mais experiente (walks>=most e >=10)
    -> as proximas (Responde rapido, Perto de voce, etc.) sao cortadas.
    """
    item = {
        "rating_average": 5.0,
        "reviews_count": 50,
        "total_walks": 100,
        "proximity_score": 95,
        "final_matching_score": 90,
        "behavior_details": {"response_time_score": 95},
    }
    ctx = {"best_rating": 5.0, "most_walks": 100}
    badges = generate_badges(item, rank=1, ranking_context=ctx)
    assert len(badges) == 3
    assert badges == ["Mais recomendado", "Melhor avaliacao", "Mais experiente"]


def test_ordem_dos_badges_preservada():
    """Quando dispara <=3 condicoes, mantem a ordem do codigo."""
    item = {
        "rating_average": 0,
        "reviews_count": 3,
        "total_walks": 5,
        "proximity_score": 90,  # Perto de voce
        "final_matching_score": 0,
        "behavior_details": {"response_time_score": 85},  # Responde rapido
    }
    ctx = {"best_rating": 5, "most_walks": 100}
    badges = generate_badges(item, rank=1, ranking_context=ctx)
    # rank1 (Mais recomendado), Responde rapido, Perto de voce
    assert badges == ["Mais recomendado", "Responde rapido", "Perto de voce"]


# ---------------------------------------------------------------------------
# generate_display_reason
# ---------------------------------------------------------------------------


def test_reason_mais_recomendado_e_perto():
    badges = ["Mais recomendado", "Perto de voce"]
    assert generate_display_reason({}, badges) == "Otima avaliacao e perto de voce"


def test_reason_mais_experiente():
    # mesmo que tenha Mais recomendado sozinho, sem Perto nao cai no primeiro if
    badges = ["Mais recomendado", "Mais experiente"]
    assert (
        generate_display_reason({}, badges) == "Passeador experiente na sua regiao"
    )


def test_reason_prioridade_recomendado_perto_sobre_experiente():
    # Primeiro if (recomendado + perto) tem prioridade sobre Mais experiente
    badges = ["Mais recomendado", "Perto de voce", "Mais experiente"]
    assert generate_display_reason({}, badges) == "Otima avaliacao e perto de voce"


def test_reason_melhor_avaliacao():
    assert (
        generate_display_reason({}, ["Melhor avaliacao"])
        == "Muito bem avaliado por outros tutores"
    )


def test_reason_prioridade_experiente_sobre_melhor_avaliacao():
    badges = ["Melhor avaliacao", "Mais experiente"]
    assert (
        generate_display_reason({}, badges) == "Passeador experiente na sua regiao"
    )


def test_reason_novo_no_aumigao():
    assert (
        generate_display_reason({}, ["Novo no Aumigao"])
        == "Novo no Aumigao e disponivel perto de voce"
    )


def test_reason_availability_score_alto_sem_badges_relevantes():
    item = {"availability_score": 90}
    assert (
        generate_display_reason(item, [])
        == "Boa disponibilidade para este horario"
    )


def test_reason_availability_abaixo_de_90_cai_no_default():
    item = {"availability_score": 89}
    assert generate_display_reason(item, []) == "Boa combinacao para este passeio"


def test_reason_default():
    assert generate_display_reason({}, []) == "Boa combinacao para este passeio"


def test_reason_badge_irrelevante_cai_no_default():
    # "Perto de voce" sozinho nao casa com nenhum if especifico -> default
    assert (
        generate_display_reason({}, ["Perto de voce"])
        == "Boa combinacao para este passeio"
    )
