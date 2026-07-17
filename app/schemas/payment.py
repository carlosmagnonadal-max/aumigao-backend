from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel


class PaymentCreate(BaseModel):
    walk_id: str | None = None
    # P2: valor deve ser estritamente positivo. Antes `amount: float` aceitava 0
    # ou negativo, permitindo criar pagamento de valor <= 0.
    amount: float = Field(gt=0)
    provider: str = "asaas"
    method: str = "pix"
    # Projeto A / D1: quando o passeio é elegível ao plano, o create_payment
    # devolve 409 {code: plano_disponivel} em vez de cobrar — o app oferece o
    # plano. charge_anyway=True é a escolha explícita do tutor por pagar avulso
    # (ignora o plano) e libera a cobrança normal.
    charge_anyway: bool = False


class PaymentQuoteResponse(BaseModel):
    """Cotação por tenant (R4): preço, desconto de plano e total. Sem taxa de serviço."""
    walk_price: float
    plan_discount_percent: float
    plan_discount: float
    total: float


class PaymentResponse(PaymentCreate, ORMModel):
    id: str
    tutor_id: str
    status: str
    provider_payment_id: str | None = None
    provider_status: str | None = None
    invoice_url: str | None = None
    pix_qr_code: str | None = None
    pix_copy_paste: str | None = None
    pix_expiration_date: str | None = None
    sandbox_message: str | None = None
    commission_percent: float | None = None
    platform_amount: float | None = None
    walker_amount: float | None = None
    created_at: datetime
