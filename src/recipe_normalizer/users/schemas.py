import uuid

from pydantic import BaseModel, EmailStr, Field

# Both auth endpoints feed the password straight into argon2 (64 MiB memory
# cost per verify — login runs one even for unknown emails, by design, to keep
# the timing constant). An unbounded field would therefore be a cheap
# CPU/memory amplifier, so cap it: a 422 costs nothing, an argon2 pass doesn't.
_MAX_PASSWORD_LEN = 200


class RegisterIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    display_name: str = Field(min_length=1, max_length=100)


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(max_length=_MAX_PASSWORD_LEN)


class UserOut(BaseModel):
    id: uuid.UUID
    email: str
    display_name: str
    is_admin: bool = False

    model_config = {"from_attributes": True}
