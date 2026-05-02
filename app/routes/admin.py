from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from app.core.database import get_db
from app.dependencies.auth import require_admin
from app.models.payment import Payment
from app.models.pet import Pet
from app.models.user import User
from app.models.walk import Walk
from app.models.walker_profile import WalkerProfile

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])

@router.get("/dashboard")
def dashboard(db: Session = Depends(get_db)):
    return {
        "total_tutors": db.query(User).filter(User.role.in_(["tutor", "cliente"])).count(),
        "total_pets": db.query(Pet).count(),
        "total_walkers": db.query(WalkerProfile).count(),
        "scheduled_walks": db.query(Walk).filter(Walk.status == "Agendado").count(),
        "completed_walks": db.query(Walk).filter(Walk.status == "Finalizado").count(),
        "estimated_revenue": sum(payment.amount for payment in db.query(Payment).all()),
    }

@router.get("/users")
def users(db: Session = Depends(get_db)):
    return db.query(User).all()

@router.get("/tutors")
def tutors(db: Session = Depends(get_db)):
    return db.query(User).filter(User.role.in_(["tutor", "cliente"])).all()

@router.get("/walkers")
def walkers(db: Session = Depends(get_db)):
    return db.query(WalkerProfile).all()

@router.post("/walkers/{walker_id}/approve")
def approve_walker(walker_id: str, db: Session = Depends(get_db)):
    profile = db.get(WalkerProfile, walker_id)
    if profile:
        profile.status = "approved"
        db.commit()
    return {"ok": True}

@router.post("/walkers/{walker_id}/reject")
def reject_walker(walker_id: str, payload: dict | None = None, db: Session = Depends(get_db)):
    profile = db.get(WalkerProfile, walker_id)
    if profile:
        profile.status = "rejected"
        profile.rejection_reason = (payload or {}).get("reason")
        db.commit()
    return {"ok": True}

@router.get("/walks")
def walks(db: Session = Depends(get_db)):
    return db.query(Walk).all()

@router.get("/payments")
def payments(db: Session = Depends(get_db)):
    return db.query(Payment).all()

@router.get("/walker-operations")
def walker_operations(db: Session = Depends(get_db)):
    walkers = db.query(WalkerProfile).all()
    pending_walks = db.query(Walk).filter(Walk.walker_id.is_(None), Walk.status == "Agendado").all()
    active_walks = db.query(Walk).filter(Walk.status.in_(["Indo buscar o pet", "Passeando agora"])).all()
    withdrawals = db.query(Payment).filter(Payment.provider == "pix").all()
    return {
        "walkers": walkers,
        "pending_requests": pending_walks,
        "active_walks": active_walks,
        "withdrawals": withdrawals,
        "metrics": {
            "pending_approvals": db.query(WalkerProfile).filter(WalkerProfile.status == "pending").count(),
            "approved_walkers": db.query(WalkerProfile).filter(WalkerProfile.status == "approved").count(),
            "available_requests": len(pending_walks),
            "active_walks": len(active_walks),
            "pending_withdrawals": len([item for item in withdrawals if item.status == "pending"]),
        },
    }

@router.post("/withdrawals/{payment_id}/approve")
def approve_withdrawal(payment_id: str, db: Session = Depends(get_db)):
    payment = db.get(Payment, payment_id)
    if payment:
        payment.status = "paid"
        db.commit()
    return {"ok": True}

@router.post("/withdrawals/{payment_id}/reject")
def reject_withdrawal(payment_id: str, db: Session = Depends(get_db)):
    payment = db.get(Payment, payment_id)
    if payment:
        payment.status = "rejected"
        db.commit()
    return {"ok": True}
