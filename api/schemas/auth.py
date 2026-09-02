from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import EmailStr, Field

from api.schemas.common import APIModel


class RegisterRequest(APIModel):
    email: EmailStr
    password: str = Field(min_length=10, max_length=72)
    display_name: str | None = Field(default=None, max_length=120)


class LoginRequest(APIModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=200)


class TokenResponse(APIModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class UserOut(APIModel):
    id: uuid.UUID
    email: str
    is_active: bool
    created_at: datetime
