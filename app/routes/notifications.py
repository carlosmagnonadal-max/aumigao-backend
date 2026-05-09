from fastapi import APIRouter

router = APIRouter(prefix="/notifications", tags=["notifications"])
api_router = APIRouter(prefix="/api/notifications", tags=["notifications"])


@router.get("")
@api_router.get("")
def get_notifications():
    return []

