from datetime import datetime
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.tip_integrity_flag import TipIntegrityFlag


TIP_REPUTATION_POLICY = "Gorjetas ficam no financeiro e nao alteram reputacao, matching, nivel, boost ou prioridade no MVP."


def tip_flag_payload(flag: TipIntegrityFlag) -> dict:
    return {
        "id": flag.id,
        "walker_id": flag.walker_id,
        "tutor_id": flag.tutor_id,
        "walk_id": flag.walk_id,
        "tip_amount": flag.tip_amount,
        "flag_type": flag.flag_type,
        "severity": flag.severity,
        "status": flag.status,
        "notes": flag.notes,
        "created_at": flag.created_at,
        "reviewed_at": flag.reviewed_at,
    }


def create_tip_flag(
    walker_id: str,
    tip_amount: float,
    db: Session,
    flag_type: str = "unusually_high_tip",
    severity: str = "medium",
    tutor_id: str | None = None,
    walk_id: str | None = None,
    notes: str | None = None,
) -> TipIntegrityFlag:
    flag = TipIntegrityFlag(
        id=str(uuid4()),
        walker_id=walker_id,
        tutor_id=tutor_id,
        walk_id=walk_id,
        tip_amount=tip_amount,
        flag_type=flag_type,
        severity=severity,
        status="open",
        notes=notes,
    )
    db.add(flag)
    db.commit()
    db.refresh(flag)
    return flag


def evaluate_tip_patterns(walker_id: str, db: Session) -> list[TipIntegrityFlag]:
    # MVP: only returns existing flags. Tip payments remain financial data and do not feed reputation.
    return (
        db.query(TipIntegrityFlag)
        .filter(TipIntegrityFlag.walker_id == walker_id)
        .order_by(TipIntegrityFlag.created_at.desc())
        .all()
    )


def review_tip_flag(flag_id: str, status: str, notes: str | None, db: Session) -> TipIntegrityFlag:
    flag = db.get(TipIntegrityFlag, flag_id)
    if not flag:
        raise HTTPException(status_code=404, detail="Flag de gorjeta nao encontrada")
    flag.status = status
    flag.notes = notes or flag.notes
    if status in {"reviewed", "dismissed", "confirmed"}:
        flag.reviewed_at = datetime.utcnow()
    db.commit()
    db.refresh(flag)
    return flag
