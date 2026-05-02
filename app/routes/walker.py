from datetime import datetime
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.dependencies.auth import get_current_user
from app.models.payment import Payment
from app.models.pet import Pet
from app.models.user import User
from app.models.walk import Walk
from app.models.walker_profile import WalkerProfile
from app.schemas.walker_profile import WalkerProfileCreate, WalkerProfileResponse, WalkerProfileUpdate

router = APIRouter(prefix="/walker", tags=["walker"])

DOG_PHOTOS = {
    "Thor": "https://images.unsplash.com/photo-1552053831-71594a27632d?auto=format&fit=crop&w=500&q=85",
    "Luna": "https://images.unsplash.com/photo-1551717743-49959800b1f6?auto=format&fit=crop&w=500&q=85",
    "Mel": "https://images.unsplash.com/photo-1586671267731-da2cf3ceeb80?auto=format&fit=crop&w=500&q=85",
    "Buddy": "https://images.unsplash.com/photo-1587300003388-59208cc962cb?auto=format&fit=crop&w=500&q=85",
    "Tequila": "https://images.unsplash.com/photo-1537151625747-768eb6cf92b2?auto=format&fit=crop&w=500&q=85",
}


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d/%m/%Y %H:%M"):
        try:
            return datetime.strptime(value[:19], fmt)
        except ValueError:
            continue
    return None


def _walk_time_parts(walk: Walk) -> tuple[str, str]:
    parsed = _parse_date(walk.scheduled_date)
    if parsed:
        return parsed.strftime("%H:%M"), parsed.strftime("%d/%m/%Y")
    return "18:00", walk.scheduled_date or "Hoje"


def _walk_payload(walk: Walk, db: Session) -> dict:
    pet = db.get(Pet, walk.pet_id) if walk.pet_id else None
    tutor = db.get(User, walk.tutor_id) if walk.tutor_id else None
    time, date = _walk_time_parts(walk)
    pet_name = pet.name if pet else "Pet"
    price = float(walk.price or 0)
    return {
        "id": walk.id,
        "pet_id": walk.pet_id,
        "pet_name": pet_name,
        "pet_photo_url": (pet.photo_url if pet else None) or DOG_PHOTOS.get(pet_name) or DOG_PHOTOS["Thor"],
        "breed": pet.breed if pet else "",
        "age": pet.age if pet else None,
        "weight": pet.weight if pet else None,
        "tutor_id": walk.tutor_id,
        "tutor_name": tutor.full_name if tutor else "Tutor",
        "tutor_phone": "",
        "date": date,
        "time": time,
        "scheduled_date": walk.scheduled_date,
        "duration_minutes": walk.duration_minutes,
        "duration": f"{walk.duration_minutes} min",
        "price": price,
        "price_label": f"R$ {price:.2f}".replace(".", ","),
        "status": walk.status,
        "area": walk.address_snapshot or "Pituba, Salvador - BA",
        "distance": "900m de voce",
        "type": "Individual",
        "payment_method": "Pagamento pelo app",
        "notes": walk.notes or "Levar agua sempre. Informe o tutor sobre qualquer ocorrencia.",
        "expires_in": "15 min",
        "is_frequent_client": True,
    }


def _completed_walks(user: User, db: Session) -> list[Walk]:
    return db.query(Walk).filter(Walk.walker_id == user.id, Walk.status == "Finalizado").all()


def _available_balance(user: User, db: Session) -> float:
    payments = db.query(Payment).filter(Payment.tutor_id == user.id).all()
    if payments:
        return sum(float(payment.amount or 0) for payment in payments)
    completed_total = sum(float(walk.price or 0) for walk in _completed_walks(user, db))
    return completed_total or 245.60


@router.get("/profile", response_model=WalkerProfileResponse | None)
def get_profile(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return db.query(WalkerProfile).filter(WalkerProfile.user_id == user.id).first()


@router.post("/profile", response_model=WalkerProfileResponse)
def create_profile(payload: WalkerProfileCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    profile = db.query(WalkerProfile).filter(WalkerProfile.user_id == user.id).first()
    if profile:
        return update_profile(payload, user, db)
    user.role = "walker"
    profile = WalkerProfile(id=str(uuid4()), user_id=user.id, **payload.model_dump())
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


@router.put("/profile", response_model=WalkerProfileResponse)
def update_profile(payload: WalkerProfileUpdate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    profile = db.query(WalkerProfile).filter(WalkerProfile.user_id == user.id).first()
    if not profile:
        profile = WalkerProfile(id=str(uuid4()), user_id=user.id)
        db.add(profile)
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(profile, key, value)
    db.commit()
    db.refresh(profile)
    return profile


@router.get("/dashboard")
def dashboard(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    active = db.query(Walk).filter(Walk.walker_id == user.id, Walk.status.in_(["Indo buscar o pet", "Passeando agora"])).all()
    accepted = db.query(Walk).filter(Walk.walker_id == user.id).all()
    available = db.query(Walk).filter(Walk.walker_id.is_(None), Walk.status == "Agendado").all()
    completed = _completed_walks(user, db)
    today_total = sum(float(walk.price or 0) for walk in completed) or 55.86
    potential = sum(float(walk.price or 0) for walk in available[:3]) or 180.0
    active_walk = _walk_payload(active[0], db) if active else (_walk_payload(accepted[0], db) if accepted else None)
    return {
        "available_requests": len(available),
        "active_walks": len(active),
        "accepted_walks": len(accepted),
        "today_earnings": today_total,
        "potential_earnings": potential,
        "level": "GOLD",
        "next_level": "ELITE",
        "score": 87,
        "level_progress": 72,
        "bonus_missing_walks": max(0, 14 - (len(completed) or 11)),
        "boost_credits": 24,
        "next_request": _walk_payload(available[0], db) if available else None,
        "active_walk": active_walk,
        "week": [
            {"day": "Seg", "date": "19", "status": "available"},
            {"day": "Ter", "date": "20", "status": "available"},
            {"day": "Qua", "date": "21", "status": "unavailable"},
            {"day": "Qui", "date": "22", "status": "available"},
            {"day": "Sex", "date": "23", "status": "partial"},
            {"day": "Sab", "date": "24", "status": "available"},
            {"day": "Dom", "date": "25", "status": "partial"},
        ],
    }


@router.get("/earnings")
def earnings(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    completed = _completed_walks(user, db)
    total = sum(float(walk.price or 0) for walk in completed)
    tips = 52.0
    transactions = []
    for walk in completed:
        payload = _walk_payload(walk, db)
        transactions.append({
            "id": f"walk-{walk.id}",
            "type": "walk",
            "description": "Passeio concluido",
            "pet_name": payload["pet_name"],
            "duration": payload["duration"],
            "date": payload["date"],
            "time": payload["time"],
            "amount": float(walk.price or 0),
            "status": "paid",
        })
    if not transactions:
        transactions = [
            {"id": "demo-walk-1", "type": "walk", "description": "Passeio concluido", "pet_name": "Thor", "duration": "60 min", "date": "19/05/2025", "time": "18:20", "amount": 35.0, "status": "paid"},
            {"id": "demo-tip-1", "type": "tip", "description": "Gorjeta recebida", "pet_name": "Thor", "duration": "", "date": "19/05/2025", "time": "18:20", "amount": 10.0, "status": "paid"},
            {"id": "demo-withdraw-1", "type": "withdraw", "description": "Saque via PIX", "pet_name": "", "duration": "", "date": "17/04/2025", "time": "21:30", "amount": -120.0, "status": "paid"},
        ]
    weekly_total = total or 420.0
    return {
        "available_balance": _available_balance(user, db),
        "weekly_total": weekly_total,
        "completed_walks": len(completed) or 11,
        "tips": tips,
        "walk_earnings": weekly_total - tips,
        "goal_total_walks": 14,
        "goal_bonus": 120.0,
        "level": "Ouro",
        "score": 87,
        "transactions": transactions,
    }


@router.get("/availability")
def availability(user: User = Depends(get_current_user)):
    return {
        "week": [
            {"day": "Seg 22", "status": "available", "possible_walks": 3},
            {"day": "Ter 23", "status": "unavailable", "possible_walks": 0},
            {"day": "Qua 24", "status": "partial", "possible_walks": 2},
            {"day": "Qui 25", "status": "available", "possible_walks": 4},
            {"day": "Sex 26", "status": "available", "possible_walks": 3},
        ],
        "slots": ["07:00", "08:00", "09:00", "14:00", "15:00", "17:00", "18:00", "19:00", "20:00"],
        "month": {
            "label": "Abril 2026",
            "estimated_earnings": 3240,
            "possible_walks": 42,
            "available_days": 20,
        },
    }


@router.put("/availability")
def update_availability(payload: dict, user: User = Depends(get_current_user)):
    return {"ok": True, "user_id": user.id, **payload}


@router.get("/requests")
def requests(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    walks = db.query(Walk).filter(Walk.walker_id.is_(None), Walk.status == "Agendado").all()
    return [_walk_payload(walk, db) for walk in walks]


@router.get("/walks")
def walker_walks(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    walks = db.query(Walk).filter(Walk.walker_id == user.id).all()
    return [_walk_payload(walk, db) for walk in walks]


@router.post("/walks/{walk_id}/accept")
def accept_walk(walk_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    walk = db.get(Walk, walk_id)
    if not walk:
        raise HTTPException(status_code=404, detail="Passeio nao encontrado")
    walk.walker_id = user.id
    walk.status = "Agendado"
    db.commit()
    return {"ok": True, "walk_id": walk_id, "walk": _walk_payload(walk, db)}


@router.post("/walks/{walk_id}/decline")
def decline_walk(walk_id: str):
    return {"ok": True, "walk_id": walk_id}


@router.post("/walks/{walk_id}/status")
def walker_status(walk_id: str, payload: dict, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    walk = db.get(Walk, walk_id)
    if not walk:
        raise HTTPException(status_code=404, detail="Passeio nao encontrado")
    if walk.walker_id not in {None, user.id}:
        raise HTTPException(status_code=403, detail="Passeio nao pertence ao passeador")
    walk.walker_id = user.id
    walk.status = payload.get("status", walk.status)
    db.commit()
    return {"ok": True, "status": walk.status, "walk": _walk_payload(walk, db)}


@router.post("/walks/{walk_id}/report")
def send_report(walk_id: str, payload: dict | None = None, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    walk = db.get(Walk, walk_id)
    if not walk:
        raise HTTPException(status_code=404, detail="Passeio nao encontrado")
    if walk.walker_id != user.id:
        raise HTTPException(status_code=403, detail="Passeio nao pertence ao passeador")
    walk.status = "Finalizado"
    db.add(Payment(id=str(uuid4()), tutor_id=user.id, walk_id=walk.id, amount=float(walk.price or 0), status="paid", provider="internal"))
    db.commit()
    return {"ok": True, "walk_id": walk_id, "status": walk.status, "report": payload or {}}


@router.post("/withdrawals")
def request_withdrawal(payload: dict, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    amount = float(payload.get("amount") or 0)
    if amount < 20:
        raise HTTPException(status_code=400, detail="Valor minimo para saque e R$ 20,00")
    balance = _available_balance(user, db)
    if amount > balance:
        raise HTTPException(status_code=400, detail="Saldo insuficiente")
    payment = Payment(id=str(uuid4()), tutor_id=user.id, walk_id=None, amount=-amount, status="pending", provider="pix")
    db.add(payment)
    db.commit()
    return {"ok": True, "withdrawal_id": payment.id, "amount": amount, "status": "pending"}
