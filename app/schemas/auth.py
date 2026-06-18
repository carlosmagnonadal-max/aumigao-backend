from pydantic import BaseModel, Field
from app.schemas.user import UserResponse


class LoginRequest(BaseModel):
    # Sec-P3: max_length defensivos — anti-DoS/log-injection.
    email: str = Field(..., max_length=254)
    password: str = Field(..., max_length=128)


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str = ""
    token_type: str = "bearer"
    user: UserResponse


class SocialLoginPayload(BaseModel):
    provider: str = Field(..., max_length=50)   # "google" | "apple"
    token: str = Field(..., max_length=4096)     # access_token (Google) ou identity_token JWT (Apple)
    email: str | None = Field(None, max_length=254)      # fallback: Apple não repete email após 1ª vez
    full_name: str | None = Field(None, max_length=200)  # nome completo da Apple
    app_target: str | None = Field(None, max_length=50)  # "walker" | "tutor" | "combined" — define role na criação
