import hashlib
import secrets
from typing import Protocol

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

_hasher = PasswordHasher()


class AuthProvider(Protocol):
    """v1: email+password. SaaS swaps in OAuth behind this seam."""

    def hash_password(self, password: str) -> str: ...
    def verify_password(self, password: str, password_hash: str) -> bool: ...


class PasswordAuthProvider:
    def hash_password(self, password: str) -> str:
        return _hasher.hash(password)

    def verify_password(self, password: str, password_hash: str) -> bool:
        try:
            return _hasher.verify(password_hash, password)
        except VerifyMismatchError:
            return False


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
