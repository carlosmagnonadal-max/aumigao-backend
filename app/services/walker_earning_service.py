"""Serviço do ledger-fornecedor do passeador da rede (Fase 2)."""
from datetime import datetime, timedelta, timezone


def compute_payable_at(completion_dt: datetime) -> datetime:
    """Cadência SEMANAL: ganhos de passeios concluídos numa semana (seg–dom)
    ficam disponíveis na QUARTA-FEIRA da semana SEGUINTE.

    Determinístico (não usa 'now'): depende só da data de conclusão.
    Retorna datetime tz-aware (UTC) à meia-noite da quarta-feira alvo.
    """
    d = completion_dt.date()
    monday_this_week = d - timedelta(days=d.weekday())  # weekday(): seg=0
    wednesday_next_week = monday_this_week + timedelta(days=7 + 2)
    return datetime(
        wednesday_next_week.year, wednesday_next_week.month, wednesday_next_week.day,
        tzinfo=timezone.utc,
    )
