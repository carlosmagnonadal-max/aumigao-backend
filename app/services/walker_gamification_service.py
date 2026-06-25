"""Serviço de eventos de gamificação do passeador.

Registra eventos do ciclo de vida da gamificação (ganho de CR, badges, missões).
NÃO commita — o caller commita junto com a ação para garantir atomicidade.
"""
from __future__ import annotations

import uuid
from typing import Optional

from sqlalchemy.orm import Session

from app.models.walker_gamification_event import WalkerGamificationEvent


def log_event(
    db: Session,
    walker_user_id: str,
    event_type: str,
    title: str,
    *,
    description: str = "",
    cr_amount: Optional[int] = None,
    related_entity_type: Optional[str] = None,
    related_entity_id: Optional[str] = None,
) -> WalkerGamificationEvent:
    """Registra um evento de gamificação para o passeador.

    Adiciona o registro à sessão mas NÃO commita — o caller commita junto com
    a ação principal, garantindo atomicidade.

    Args:
        db: Sessão SQLAlchemy.
        walker_user_id: ID do usuário passeador.
        event_type: Tipo do evento (ex.: "cr_granted", "badge_earned", "level_up",
            "mission_completed", "boost_activated").
        title: Título legível do evento.
        description: Descrição opcional.
        cr_amount: Quantidade de CR envolvida (None se não aplicável).
        related_entity_type: Tipo da entidade relacionada (ex.: "walk").
        related_entity_id: ID da entidade relacionada.

    Returns:
        WalkerGamificationEvent recém-criado (ainda não persistido).
    """
    event = WalkerGamificationEvent(
        id=str(uuid.uuid4()),
        walker_user_id=walker_user_id,
        event_type=event_type,
        title=title,
        description=description,
        cr_amount=cr_amount,
        related_entity_type=related_entity_type,
        related_entity_id=related_entity_id,
    )
    db.add(event)
    return event


def list_events(
    db: Session,
    walker_user_id: str,
    limit: int = 50,
) -> list[WalkerGamificationEvent]:
    """Lista eventos de gamificação do passeador em ordem decrescente de criação.

    Args:
        db: Sessão SQLAlchemy.
        walker_user_id: ID do usuário passeador.
        limit: Número máximo de eventos a retornar (padrão 50).

    Returns:
        Lista de WalkerGamificationEvent ordenada do mais recente ao mais antigo.
    """
    return (
        db.query(WalkerGamificationEvent)
        .filter(WalkerGamificationEvent.walker_user_id == walker_user_id)
        .order_by(WalkerGamificationEvent.created_at.desc())
        .limit(limit)
        .all()
    )
