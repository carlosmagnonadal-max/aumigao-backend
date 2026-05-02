from uuid import uuid4
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.core.database import get_db
from app.dependencies.auth import get_current_user
from app.models.walk import Walk
from app.models.user import User
from app.schemas.walk import WalkCreate, WalkResponse, WalkUpdateStatus

router = APIRouter(prefix="/walks", tags=["walks"])

@router.get("", response_model=list[WalkResponse])
def list_walks(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    query = db.query(Walk)
    if user.role == "walker":
        query = query.filter((Walk.walker_id == user.id) | (Walk.walker_id.is_(None)))
    elif user.role not in {"admin", "super_admin"}:
        query = query.filter(Walk.tutor_id == user.id)
    return query.order_by(Walk.created_at.desc()).all()

@router.post("", response_model=WalkResponse)
def create_walk(payload: WalkCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    walk = Walk(id=str(uuid4()), tutor_id=user.id, **payload.model_dump())
    db.add(walk)
    db.commit()
    db.refresh(walk)
    return walk

@router.get("/{walk_id}", response_model=WalkResponse)
def get_walk(walk_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    walk = db.get(Walk, walk_id)
    if not walk:
        raise HTTPException(status_code=404, detail="Passeio nao encontrado")
    if user.role not in {"admin", "super_admin"} and walk.tutor_id != user.id and walk.walker_id != user.id:
        raise HTTPException(status_code=403, detail="Sem permissao")
    return walk

@router.put("/{walk_id}/status", response_model=WalkResponse)
def update_status(walk_id: str, payload: WalkUpdateStatus, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    walk = get_walk(walk_id, user, db)
    walk.status = payload.status
    db.commit()
    db.refresh(walk)
    return walk

@router.delete("/{walk_id}")
def delete_walk(walk_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    walk = get_walk(walk_id, user, db)
    db.delete(walk)
    db.commit()
    return {"ok": True}
