from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.core.database import Base, engine
from app.models import Payment, Pet, TutorProfile, User, Walk, WalkerProfile
from app.routes import admin, auth, payments, pets, tutor, walker, walks

Base.metadata.create_all(bind=engine)

app = FastAPI(title="Aumigao Walk API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

try:
    app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")
except RuntimeError:
    pass

app.include_router(auth.router)
app.include_router(tutor.router)
app.include_router(pets.router)
app.include_router(walks.router)
app.include_router(walker.router)
app.include_router(payments.router)
app.include_router(admin.router)

@app.get("/")
def root():
    return {"message": "Aumigao Walk API rodando"}
