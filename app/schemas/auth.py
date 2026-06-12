from pydantic import BaseModel
from app.schemas.user import UserResponse


class LoginRequest(BaseModel):
    email: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str = ""
    token_type: str = "bearer"
    user: UserResponse


class SocialLoginPayload(BaseModel):
    provider: str        # "google" | "apple"
    token: str           # access_token (Google) ou identity_token JWT (Apple)
    email: str | None = None      # fallback: Apple não repete email após 1ª vez
    full_name: str | None = None  # nome completo da Apple
    app_target: str | None = None # "walker" | "tutor" | "combined" — define role na criação
