from uuid import uuid4
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from app.core.database import get_db
from app.dependencies.auth import get_current_user
from app.models.payment import Payment
from app.models.user import User
from app.schemas.payment import PaymentCreate, PaymentResponse

router = APIRouter(prefix="/payments", tags=["payments"])

@router.post("/create", response_model=PaymentResponse)
def create_payment(payload: PaymentCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    payment = Payment(id=str(uuid4()), tutor_id=user.id, status="pending", **payload.model_dump())
    db.add(payment)
    db.commit()
    db.refresh(payment)
    return payment

@router.get("/{payment_id}", response_model=PaymentResponse)
def get_payment(payment_id: str, db: Session = Depends(get_db)):
    return db.get(Payment, payment_id)

@router.post("/webhooks/asaas")
def asaas_webhook(payload: dict):
    return {"ok": True, "received": payload.get("event")}
