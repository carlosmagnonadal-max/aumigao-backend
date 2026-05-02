from datetime import datetime
from pydantic import BaseModel
from app.schemas.common import ORMModel

class PaymentCreate(BaseModel):
    walk_id: str | None = None
    amount: float
    provider: str = "asaas"

class PaymentResponse(PaymentCreate, ORMModel):
    id: str
    tutor_id: str
    status: str
    provider_payment_id: str | None = None
    created_at: datetime
