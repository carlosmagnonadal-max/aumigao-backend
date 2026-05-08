def generate_badges(item: dict, rank: int, ranking_context: dict) -> list[str]:
    badges: list[str] = []
    if rank == 1:
        badges.append("Mais recomendado")
    if item.get("rating_average", 0) >= ranking_context.get("best_rating", 0) and item.get("reviews_count", 0) >= 5:
        badges.append("Melhor avaliacao")
    if item.get("total_walks", 0) >= ranking_context.get("most_walks", 0) and item.get("total_walks", 0) >= 10:
        badges.append("Mais experiente")
    if item.get("behavior_details", {}).get("response_time_score", 0) >= 80:
        badges.append("Responde rapido")
    if item.get("proximity_score", 0) >= 85:
        badges.append("Perto de voce")
    if item.get("proximity_score", 0) >= 70 and item.get("final_matching_score", 0) >= 75:
        badges.append("Destaque da regiao")
    if item.get("total_walks", 0) < 5 and item.get("reviews_count", 0) <= 2:
        badges.append("Novo no Aumigao")
    return badges[:3]


def generate_display_reason(item: dict, badges: list[str]) -> str:
    if "Mais recomendado" in badges and "Perto de voce" in badges:
        return "Otima avaliacao e perto de voce"
    if "Mais experiente" in badges:
        return "Passeador experiente na sua regiao"
    if "Melhor avaliacao" in badges:
        return "Muito bem avaliado por outros tutores"
    if "Novo no Aumigao" in badges:
        return "Novo no Aumigao e disponivel perto de voce"
    if item.get("availability_score", 0) >= 90:
        return "Boa disponibilidade para este horario"
    return "Boa combinacao para este passeio"
