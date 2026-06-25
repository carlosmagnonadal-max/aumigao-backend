"""Serviço de carteira de CR (Créditos de Reputação) do passeador.

Gerencia saldo, ganhos, gastos e penalidades de CR. NÃO commita transações —
o caller commita junto com a ação principal para garantir atomicidade.
A única exceção é mark_read no serviço de notificações, que é uma ação isolada.

Decisão de design — clamp do penalty:
    O balance nunca vai abaixo de 0 (max(0, balance - amount)), mas a transação
    registra o valor CHEIO da penalidade. Razão: evita saldo negativo visível
    para o passeador (melhor UX) mas mantém a penalidade inteira no log para
    fins de auditoria e relatório. O lifetime_earned e lifetime_spent NÃO são
    afetados por penalidades (são rastreados separadamente pelo tx_type).

Importação circular:
    walker_gamification_service é importado dentro das funções (import tardio)
    para evitar ciclo de importação caso o gamification_service venha a importar
    este módulo no futuro.
"""
from __future__ import annotations

import uuid
from typing import Optional

from sqlalchemy.orm import Session

from app.models.walker_cr_transaction import WalkerCrTransaction
from app.models.walker_cr_wallet import WalkerCrWallet


def already_awarded(
    db: Session,
    walker_user_id: str,
    source: str,
    related_entity_id: str,
) -> bool:
    """Verifica se um CR já foi concedido para esta entidade específica.

    Garante idempotência: retorna True se já existe WalkerCrTransaction com
    (walker_user_id, source, related_entity_id) iguais. Use antes de cada
    earn/penalty ligado a uma entidade para evitar concessão dupla em caso
    de reprocessamento.

    Args:
        db: Sessão SQLAlchemy.
        walker_user_id: ID do usuário passeador.
        source: Origem da transação (ex.: "walk_completed", "review_5star").
        related_entity_id: ID da entidade relacionada (ex.: walk.id, review.id).

    Returns:
        True se já existe transação com esses parâmetros, False caso contrário.
    """
    return (
        db.query(WalkerCrTransaction)
        .filter(
            WalkerCrTransaction.walker_user_id == walker_user_id,
            WalkerCrTransaction.source == source,
            WalkerCrTransaction.related_entity_id == related_entity_id,
        )
        .first()
    ) is not None


def get_or_create_wallet(db: Session, walker_user_id: str) -> WalkerCrWallet:
    """Retorna a carteira de CR do passeador, criando-a com saldo 0 se não existir.

    Faz flush para que o ID esteja disponível na sessão sem commitar.

    Args:
        db: Sessão SQLAlchemy.
        walker_user_id: ID do usuário passeador.

    Returns:
        WalkerCrWallet existente ou recém-criado.
    """
    wallet = (
        db.query(WalkerCrWallet)
        .filter(WalkerCrWallet.walker_user_id == walker_user_id)
        .first()
    )
    if wallet is None:
        wallet = WalkerCrWallet(
            id=str(uuid.uuid4()),
            walker_user_id=walker_user_id,
            balance=0,
            lifetime_earned=0,
            lifetime_spent=0,
        )
        db.add(wallet)
        db.flush()
    return wallet


def get_balance(db: Session, walker_user_id: str) -> int:
    """Retorna o saldo atual de CR do passeador.

    Cria a carteira com saldo 0 se ainda não existir.

    Args:
        db: Sessão SQLAlchemy.
        walker_user_id: ID do usuário passeador.

    Returns:
        Saldo atual em CR (inteiro >= 0).
    """
    wallet = get_or_create_wallet(db, walker_user_id)
    return wallet.balance


def earn_cr(
    db: Session,
    walker_user_id: str,
    amount: int,
    source: str,
    *,
    description: str = "",
    related_entity_type: Optional[str] = None,
    related_entity_id: Optional[str] = None,
    log_event: bool = True,
) -> WalkerCrTransaction:
    """Credita CR na carteira do passeador.

    Cria uma transação do tipo "earn", incrementa balance e lifetime_earned.
    Se log_event=True, registra um WalkerGamificationEvent do tipo "cr_granted".
    NÃO commita — o caller commita junto com a ação principal.

    Args:
        db: Sessão SQLAlchemy.
        walker_user_id: ID do usuário passeador.
        amount: Quantidade de CR a creditar (deve ser positivo).
        source: Origem do crédito (ex.: "walk_completed", "review_5star").
        description: Descrição opcional da transação.
        related_entity_type: Tipo da entidade relacionada (ex.: "walk").
        related_entity_id: ID da entidade relacionada.
        log_event: Se True, registra evento de gamificação.

    Returns:
        WalkerCrTransaction criada (ainda não persistida no banco).
    """
    wallet = get_or_create_wallet(db, walker_user_id)
    wallet.balance += amount
    wallet.lifetime_earned += amount

    tx = WalkerCrTransaction(
        id=str(uuid.uuid4()),
        walker_user_id=walker_user_id,
        amount=amount,
        tx_type="earn",
        source=source,
        description=description,
        related_entity_type=related_entity_type,
        related_entity_id=related_entity_id,
    )
    db.add(tx)

    if log_event:
        # Import tardio para evitar ciclo de importação.
        from app.services.walker_gamification_service import log_event as _log_event

        _log_event(
            db,
            walker_user_id,
            event_type="cr_granted",
            title=f"+{amount} CR ({source})",
            description=description,
            cr_amount=amount,
            related_entity_type=related_entity_type,
            related_entity_id=related_entity_id,
        )

    return tx


def spend_cr(
    db: Session,
    walker_user_id: str,
    amount: int,
    source: str,
    *,
    description: str = "",
    related_entity_type: Optional[str] = None,
    related_entity_id: Optional[str] = None,
    log_event: bool = True,
) -> Optional[WalkerCrTransaction]:
    """Debita CR da carteira do passeador (gasto voluntário, ex.: boost).

    Valida o saldo antes de debitar. Se insuficiente, retorna None sem alterar
    o saldo — o caller é responsável por retornar HTTP 400 ao cliente.
    Se ok: cria transação com amount negativo, decrementa balance e incrementa
    lifetime_spent. NÃO commita.

    Args:
        db: Sessão SQLAlchemy.
        walker_user_id: ID do usuário passeador.
        amount: Quantidade de CR a debitar (positivo — será negado na transação).
        source: Origem do débito (ex.: "boost_24h").
        description: Descrição opcional.
        related_entity_type: Tipo da entidade relacionada.
        related_entity_id: ID da entidade relacionada.
        log_event: Se True, registra evento de gamificação "boost_activated".

    Returns:
        WalkerCrTransaction criada, ou None se saldo insuficiente.
    """
    wallet = get_or_create_wallet(db, walker_user_id)
    if wallet.balance < amount:
        return None

    wallet.balance -= amount
    wallet.lifetime_spent += amount

    tx = WalkerCrTransaction(
        id=str(uuid.uuid4()),
        walker_user_id=walker_user_id,
        amount=-amount,
        tx_type="spend",
        source=source,
        description=description,
        related_entity_type=related_entity_type,
        related_entity_id=related_entity_id,
    )
    db.add(tx)

    if log_event:
        from app.services.walker_gamification_service import log_event as _log_event

        _log_event(
            db,
            walker_user_id,
            event_type="boost_activated",
            title=f"-{amount} CR ({source})",
            description=description,
            cr_amount=-amount,
            related_entity_type=related_entity_type,
            related_entity_id=related_entity_id,
        )

    return tx


def penalty_cr(
    db: Session,
    walker_user_id: str,
    amount: int,
    source: str,
    *,
    description: str = "",
    related_entity_type: Optional[str] = None,
    related_entity_id: Optional[str] = None,
    log_event: bool = True,
) -> WalkerCrTransaction:
    """Aplica penalidade de CR na carteira do passeador.

    Decisão de design — clamp em 0:
        O balance é limitado a max(0, balance - amount): o passeador nunca vê
        saldo negativo (melhor UX e evita dívida de CR). A transação, porém,
        registra o valor CHEIO da penalidade (amount negativo), preservando a
        auditoria completa independente do clamp.

    NÃO afeta lifetime_spent (penalidades são rastreadas pelo tx_type="penalty",
    não como gastos voluntários). NÃO commita.

    Args:
        db: Sessão SQLAlchemy.
        walker_user_id: ID do usuário passeador.
        amount: Quantidade de CR a penalizar (positivo — será negado na transação).
        source: Origem da penalidade (ex.: "no_show").
        description: Descrição opcional.
        related_entity_type: Tipo da entidade relacionada.
        related_entity_id: ID da entidade relacionada.
        log_event: Se True, registra evento de gamificação "cr_penalty".

    Returns:
        WalkerCrTransaction criada (com amount = -amount, refletindo o valor cheio).
    """
    wallet = get_or_create_wallet(db, walker_user_id)
    # Clamp: balance não vai abaixo de 0, mas a transação guarda o valor cheio.
    wallet.balance = max(0, wallet.balance - amount)

    tx = WalkerCrTransaction(
        id=str(uuid.uuid4()),
        walker_user_id=walker_user_id,
        amount=-amount,
        tx_type="penalty",
        source=source,
        description=description,
        related_entity_type=related_entity_type,
        related_entity_id=related_entity_id,
    )
    db.add(tx)

    if log_event:
        from app.services.walker_gamification_service import log_event as _log_event

        _log_event(
            db,
            walker_user_id,
            event_type="cr_penalty",
            title=f"-{amount} CR ({source})",
            description=description,
            cr_amount=-amount,
            related_entity_type=related_entity_type,
            related_entity_id=related_entity_id,
        )

    return tx


def list_transactions(
    db: Session,
    walker_user_id: str,
    limit: int = 100,
) -> list[WalkerCrTransaction]:
    """Lista transações de CR do passeador em ordem decrescente de criação.

    Args:
        db: Sessão SQLAlchemy.
        walker_user_id: ID do usuário passeador.
        limit: Número máximo de transações a retornar (padrão 100).

    Returns:
        Lista de WalkerCrTransaction ordenada do mais recente ao mais antigo.
    """
    return (
        db.query(WalkerCrTransaction)
        .filter(WalkerCrTransaction.walker_user_id == walker_user_id)
        .order_by(WalkerCrTransaction.created_at.desc())
        .limit(limit)
        .all()
    )
