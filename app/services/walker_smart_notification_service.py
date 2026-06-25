"""Serviço de smart notifications do passeador.

Gerencia criação, listagem, leitura e contagem de notificações inteligentes.
create_notification NÃO commita (atomicidade com a ação principal).
mark_read commita (ação isolada de leitura).
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.models.walker_smart_notification import WalkerSmartNotification


def create_notification(
    db: Session,
    walker_user_id: str,
    notification_type: str,
    title: str,
    *,
    message: str = "",
    priority: str = "normal",
    trigger_source: str,
    expires_at: Optional[datetime] = None,
) -> WalkerSmartNotification:
    """Cria uma notificação inteligente para o passeador.

    Registra sent_at como utcnow e adiciona à sessão sem commitar.
    O caller commita junto com a ação que originou a notificação.

    Args:
        db: Sessão SQLAlchemy.
        walker_user_id: ID do usuário passeador.
        notification_type: Tipo da notificação (ex.: "cr_earned", "boost_available").
        title: Título exibido ao passeador.
        message: Corpo opcional da mensagem.
        priority: Prioridade ("low", "normal", "high"). Padrão "normal".
        trigger_source: Origem que disparou a notificação (ex.: "walk_completed").
        expires_at: Data/hora de expiração (None = sem expiração).

    Returns:
        WalkerSmartNotification criada (ainda não persistida no banco).
    """
    notification = WalkerSmartNotification(
        id=str(uuid.uuid4()),
        walker_user_id=walker_user_id,
        notification_type=notification_type,
        title=title,
        message=message,
        priority=priority,
        trigger_source=trigger_source,
        sent_at=datetime.utcnow(),
        expires_at=expires_at,
    )
    db.add(notification)
    return notification


def list_notifications(
    db: Session,
    walker_user_id: str,
    *,
    unread_only: bool = False,
    limit: int = 50,
) -> list[WalkerSmartNotification]:
    """Lista notificações do passeador ordenadas por sent_at decrescente.

    Args:
        db: Sessão SQLAlchemy.
        walker_user_id: ID do usuário passeador.
        unread_only: Se True, retorna apenas notificações não lidas (read_at IS NULL).
        limit: Número máximo de notificações a retornar (padrão 50).

    Returns:
        Lista de WalkerSmartNotification ordenada da mais recente à mais antiga.
    """
    query = db.query(WalkerSmartNotification).filter(
        WalkerSmartNotification.walker_user_id == walker_user_id
    )
    if unread_only:
        query = query.filter(WalkerSmartNotification.read_at.is_(None))
    return (
        query.order_by(WalkerSmartNotification.sent_at.desc())
        .limit(limit)
        .all()
    )


def mark_read(
    db: Session,
    notification_id: str,
    walker_user_id: str,
) -> Optional[WalkerSmartNotification]:
    """Marca uma notificação como lida, validando a propriedade pelo walker_user_id.

    Valida que a notificação pertence ao passeador antes de alterar. Se não
    encontrada ou ownership inválido, retorna None (o caller retorna 404).
    Commita a alteração (ação isolada — não está dentro de uma transação maior).

    Args:
        db: Sessão SQLAlchemy.
        notification_id: ID da notificação a marcar como lida.
        walker_user_id: ID do usuário passeador (validação de ownership).

    Returns:
        WalkerSmartNotification atualizada, ou None se não encontrada/ownership inválido.
    """
    notification = (
        db.query(WalkerSmartNotification)
        .filter(
            WalkerSmartNotification.id == notification_id,
            WalkerSmartNotification.walker_user_id == walker_user_id,
        )
        .first()
    )
    if notification is None:
        return None

    notification.read_at = datetime.utcnow()
    db.commit()
    db.refresh(notification)
    return notification


def count_unread(db: Session, walker_user_id: str) -> int:
    """Conta notificações não lidas (read_at IS NULL) do passeador.

    Args:
        db: Sessão SQLAlchemy.
        walker_user_id: ID do usuário passeador.

    Returns:
        Número de notificações não lidas.
    """
    return (
        db.query(WalkerSmartNotification)
        .filter(
            WalkerSmartNotification.walker_user_id == walker_user_id,
            WalkerSmartNotification.read_at.is_(None),
        )
        .count()
    )
